"""Turning "which account is this call about" into a token that can see it.

The second half of App authentication: `app_auth` signs the JWT that proves this process is the
App, and this trades that JWT for a token scoped to one installation, kept until it expires.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote

import httpx

from shannon.domain.json import is_json_list, is_json_object
from shannon.github.app_auth import app_configured, app_jwt
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from shannon.github.mapping import parse_timestamp
from shannon.github.responses import json_object
from shannon.github.transport import github_headers, is_rate_limited, retry_after

logger = logging.getLogger(__name__)

# How long before a token expires it is thrown away and minted again. GitHub gives an hour, and
# five minutes covers a slow request, a retry behind it and a clock that disagrees.
REFRESH_MARGIN = timedelta(minutes=5)

# How far the installation list is followed. At GitHub's maximum of a hundred rows a page this is
# a thousand accounts, past any deployment this bot is for, and bounded rather than trusted
# because a Link header pointing at itself would otherwise read for as long as the process lives.
# The client bounds its own paging for the same reason and at a different number; this one is
# about how many accounts an App is installed on rather than how long a list of cards can be.
MAX_INSTALLATION_PAGES = 10


class Knows(StrEnum):
    """What the installation map has to say about an account."""

    AN_INSTALLATION = "an installation"
    A_SUSPENSION = "a suspension"
    NOTHING = "nothing"


@dataclass(frozen=True, slots=True)
class MapSays:
    """What the map holds for an account, and the id where there is one to mint against.

    Three answers rather than two, and the third is the whole reason this type exists. `int | None`
    folded "a row this bot HAS and will not mint against" into "a row this bot does not have",
    which cost nothing for as long as nothing could act on the difference. The moment something
    could ask GitHub about the second, that fold turned a paused App into a GitHub request and a
    database write on every single call, for as long as it stayed paused.
    """

    knows: Knows
    # Zero rather than None for the two answers that have no id: there is nothing to mint against
    # in either, and `knows` is what a caller is required to read first.
    installation_id: int = 0


@dataclass(frozen=True, slots=True)
class Installation:
    """The installation GitHub says covers a repository, and whether it is paused.

    Both, because the map keeps both, and a writer handed only the id has to guess the other.
    """

    installation_id: int
    suspended: bool


class ResolvesInstallations(Protocol):
    """Which installation covers a GitHub account, and somewhere to keep a new answer.

    A reader and two writers rather than a reader alone, because the answer has to be learnable
    and un-learnable. An empty map is not evidence that nothing is installed - it is a bot that
    was never told - and a row naming an installation GitHub no longer has is worse than no row,
    because it answers confidently and shuts the asking out.
    """

    async def installation_for(self, owner: str) -> MapSays: ...

    async def remember(
        self,
        *,
        installation_id: int,
        account_login: str,
        account_id: int | None,
        suspended: bool,
    ) -> None: ...

    async def forget(self, installation_id: int) -> None: ...


class InstallationTokens:
    """Mints installation tokens and keeps each one until it is nearly stale.

    One lock per installation, so a burst for one repository mints once without blocking another.
    """

    def __init__(
        self,
        *,
        client_id: str,
        private_key_pem: str,
        http: httpx.AsyncClient,
        directory: ResolvesInstallations,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_id = client_id
        self._private_key_pem = private_key_pem
        self._http = http
        self._directory = directory
        self._now = now
        self._minted: dict[int, tuple[str, datetime]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # One lock for all discovery rather than one per owner. Discovery happens on a miss, so
        # a cold map means a burst of callers all missing together - and the argument the mint
        # lock below makes applies here a step earlier. One rather than a map keyed on the owner
        # because the owner is not always a login this bot vouched for (a `/pr` or `/issue` link
        # naming another repository is looked up under that link's owner), so a per-owner map is
        # a dictionary anybody can grow. Two
        # different uncached owners queue behind each other, which costs one HTTP call of
        # waiting on a path that only runs when the map has nothing.
        self._discovery = asyncio.Lock()
        # None until GitHub has answered, "" once it answered with no usable slug. A refusal is
        # not kept: that would drop the install link for the life of the process, and asking again
        # costs one request per `/register`.
        self._slug: str | None = None

    def app_token(self) -> str:
        """The App's own JWT, for the handful of endpoints that take one rather than a token."""
        return app_jwt(
            client_id=self._client_id, private_key_pem=self._private_key_pem, now=self._now()
        )

    async def token_for(self, owner: str) -> str:
        # The key checked here and the JWT signed only where one is sent (#271). Every GitHub
        # request asks for a token, once per page of a paged read, and nearly all of them are
        # answered from the cache below without needing one.
        if not app_configured(client_id=self._client_id, private_key_pem=self._private_key_pem):
            return ""

        says = await self._directory.installation_for(owner)
        if says.knows is Knows.A_SUSPENSION:
            # Not asked. The map HAS this account and is refusing it, and GitHub would only
            # confirm the refusal - so asking would spend a list call, write the row back
            # unchanged, and refuse again, on every call, for as long as the App stays paused.
            return ""
        installation: int | None
        if says.knows is Knows.AN_INSTALLATION:
            installation = says.installation_id
        else:
            # Asked of GitHub rather than read as a refusal, which is what the store has said all
            # along and what nothing did. `installation.created` arrives once and is never sent
            # again for an App that is already installed, so a map holding nothing for this owner
            # - a delivery missed while this process was down, a database restored from before the
            # install - waited on some other installation event happening to arrive, or on
            # somebody running `/register` again. Neither is something an operator can summon,
            # and neither happens at all on a quiet App.
            installation = await self._discover(owner)
        if installation is None:
            return ""

        held = self._minted.get(installation)
        if held is not None and held[1] - REFRESH_MARGIN > self._now():
            return held[0]

        lock = self._locks.setdefault(installation, asyncio.Lock())
        async with lock:
            # Checked again inside the lock: without it, ten deliveries arriving together
            # each mint a token, and every mint counts against the App's rate limit.
            held = self._minted.get(installation)
            if held is not None and held[1] - REFRESH_MARGIN > self._now():
                return held[0]
            return await self._mint(installation, owner)

    async def _discover(self, owner: str) -> int | None:
        """Ask GitHub which of its installations covers this account, and keep the answer.

        Under the discovery lock, and the map is re-read inside it for the reason the mint gives:
        the callers queued behind the first one want the row it wrote rather than a second trip to
        GitHub. Which makes the answer they get either of the other two - an id somebody just
        learnt, or a suspension somebody just learnt - so both are read here rather than assumed
        away.

        An owner GitHub does not list is asked about again next time. Caching that refusal is the
        whole of the bug this undoes, and an App installed a minute from now has to be found
        without a restart. That does cost one list call per attempt, and this path runs per
        request rather than once per command the way `installed_on` does, so the cost is real and
        is the price of the map never getting permanently stuck again.
        """
        async with self._discovery:
            says = await self._directory.installation_for(owner)
            if says.knows is Knows.AN_INSTALLATION:
                return says.installation_id
            if says.knows is Knows.A_SUSPENSION:
                return None

            found = await self._covering_installation(owner)
            if found is None:
                logger.info("GitHub lists no installation of this app covering %s", owner)
                return None

            await self._directory.remember(
                installation_id=found.installation_id,
                # GitHub's own login, not the string the caller asked about. The store keys the
                # row on `account_login.strip().lower()`, so writing anything else risks a row
                # that cannot be read back by the owner it describes - and GitHub is the authority
                # on its own spelling in any case.
                account_login=found.login,
                account_id=found.account_id,
                suspended=found.suspended,
            )
            if found.suspended:
                # Written down and then refused, the way the suspend webhook writes the row before
                # applying the suspension. The write is what closes the loop: next call the map
                # answers `A_SUSPENSION`, and `token_for` stops before reaching this at all.
                logger.info(
                    "GitHub says installation %s covers %s and is suspended, so nothing can be "
                    "read for it",
                    found.installation_id,
                    found.login,
                )
                return None

            logger.info(
                "GitHub says installation %s covers %s, which this bot had not been told; "
                "writing it down",
                found.installation_id,
                found.login,
            )
            return found.installation_id

    async def _covering_installation(self, owner: str) -> _Covering | None:
        """Walk this App's own installations until the account turns up, or the list runs out.

        The whole list rather than one owner, because the per-owner endpoints are split by kind -
        `/users/{login}/installation` against `/orgs/{org}/installation` - and picking the kind in
        order to find out which kind the account is would be the wrong way round.

        Paged, following GitHub's Link header the way the client does, because reading one page
        and reporting "not installed" would state something that was never observed: at a hundred
        rows a page the first page is not the list, it is the first page. A bound rather than
        trust, and it says so when it stops, because a truncation that logs like an answer is how
        an operator ends up looking for the wrong thing.
        """
        jwt = self.app_token()
        url: str | None = "/app/installations"
        params: dict[str, int] = {"per_page": 100}
        for _ in range(MAX_INSTALLATION_PAGES):
            if url is None:
                return None

            response = await self._request("GET", url, jwt, params=params or None)
            if response.status_code >= 400:
                # Logged rather than raised, unlike `installed_on`: nobody asked this question, it
                # is a repair attempted on the way past, and failing it should leave the caller
                # with the anonymous request it would have made anyway rather than an exception.
                # Not reaching GitHub at all does raise, because that request would fail too.
                logger.warning(
                    "GitHub would not list this app's installations (%s), so nothing can be read "
                    "as %s. Check the GitHub App's id and private key",
                    response.status_code,
                    owner,
                )
                return None

            found = _covering(response, owner)
            if found is not None:
                return found

            # The next URL carries the cursor already, so the original parameters must not be
            # sent again beside it.
            url = response.links.get("next", {}).get("url")
            params = {}

        logger.warning(
            "stopped reading this app's installations after %s pages, so whether it covers %s "
            "was never settled",
            MAX_INSTALLATION_PAGES,
            owner,
        )
        return None

    async def installed_on(self, owner: str, name: str) -> Installation | None:
        """Which installation covers one repository, asked of GitHub rather than of the cache.

        A repository nobody has registered has no row, and no row means ask GitHub rather than
        not installed. A JWT, because there is no token until this has answered. GitHub 404s both
        for a repository that does not exist and for one the App was never installed on, and
        separating the two would say whether a private repository exists.

        Whether it is suspended comes back too, read off the same answer, because whoever writes
        this down would otherwise have to guess: answering with the id alone is how `/register`
        came to clear a suspension the map had been told about (#268).
        """
        jwt = self.app_token()
        if not jwt:
            return None

        response = await self._request(
            "GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/installation", jwt
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise _app_refusal(
                response,
                asking=f"the installation lookup for {owner}/{name}",
                do=f"say whether the app is installed on {owner}/{name}",
                refused=f"to say whether the app is installed on {owner}/{name}",
            )

        payload = json_object(response)
        installation = payload.get("id")
        if not isinstance(installation, int):
            return None
        # A timestamp or null, and only its presence matters here, as in `_covering`.
        return Installation(
            installation_id=installation, suspended=payload.get("suspended_at") is not None
        )

    async def app_slug(self) -> str:
        """The App's own slug, for building the "install me" link, read once and kept.

        Read from GitHub rather than configured: it never changes for a given App.
        """
        if self._slug is not None:
            return self._slug
        jwt = self.app_token()
        if not jwt:
            return ""

        # Neither failure is raised: the slug only makes a message more helpful, and `/register`
        # should not fail because the link could not be prettified.
        try:
            response = await self._request("GET", "/app", jwt)
        except GitHubUnavailableError as error:
            logger.warning("could not read the app's own slug: %s", error)
            return ""
        if response.status_code >= 400:
            logger.warning("could not read the app's own slug (%s)", response.status_code)
            return ""

        slug = json_object(response).get("slug")
        self._slug = slug if isinstance(slug, str) else ""
        return self._slug

    async def _mint(self, installation: int, owner: str) -> str:
        """Ask GitHub for a token, or answer "" for an installation that has gone.

        A 404 means the installation was removed between the directory read and this call, which
        is permanent and the same outcome as never having been installed. Everything else raises:
        a 401 is a key GitHub will not accept, and calling that "no installation" misdirects.

        Raised as what it is, by `_app_refusal`.
        """
        response = await self._request(
            "POST", f"/app/installations/{installation}/access_tokens", self.app_token()
        )
        if response.status_code == 404:
            logger.info("GitHub has no installation %s for %s any more", installation, owner)
            self._minted.pop(installation, None)
            # Forgotten as well as dropped from the cache. The row is what made the map answer
            # `AN_INSTALLATION`, so leaving it there shuts discovery out and every later call
            # posts this same 404 - and a 404 mints nothing, so there is no token to cache and
            # nothing absorbs the repeat. Forgetting turns the next call into a question, and a
            # reinstall's new id is exactly the sort of answer discovery can now find.
            await self._directory.forget(installation)
            return ""
        if response.status_code >= 400:
            raise _app_refusal(
                response,
                asking=f"installation tokens for {owner}",
                do=f"mint an installation token for {owner}",
                refused=f"an installation token for {owner}",
            )

        token, expires_at = _minted(response)
        self._minted[installation] = (token, expires_at)
        return token

    async def _request(
        self, method: str, url: str, jwt: str, *, params: Mapping[str, int] | None = None
    ) -> httpx.Response:
        """One request made as the App, with a failure to reach GitHub filed as one.

        Issue #271. The raw httpx error used to come out of here. Most reads caught it, because
        the token is asked for inside the `try` that sends them, but `/register` asks these
        questions directly and answered "Something went wrong here" to an ordinary outage.

        The headers every other request carries, set here because the HTTP client this is handed
        is shared with services that talk to somebody other than GitHub's API.
        """
        headers = {**github_headers(), "Authorization": f"Bearer {jwt}"}
        try:
            return await self._http.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc


def _app_refusal(response: httpx.Response, *, asking: str, do: str, refused: str) -> GitHubError:
    """What an answer to one of the App's own requests is, when it is not a yes.

    A rate limit and a server error are not GitHub refusing the key. Filed as a refusal they read
    as final to every caller that tells the two apart - a reply on a diff that waits out an
    unreachable GitHub went out at once instead, ringing nobody, whenever its token happened to
    fall due during the outage (found reviewing #231) - and `/register` told an admin to check the
    App's key while GitHub was down (#271).
    """
    status = response.status_code
    if status == 429 or is_rate_limited(response):
        return GitHubRateLimitError(
            f"GitHub is rate limiting {asking}", retry_after=retry_after(response)
        )
    if status >= 500:
        return GitHubUnavailableError(f"GitHub could not {do} ({status})")
    return GitHubAuthError(
        f"GitHub refused {refused} ({status}). Check the GitHub App's id and private key."
    )


@dataclass(frozen=True, slots=True)
class _Covering:
    """One row of GitHub's installation list, as much of it as is worth keeping."""

    installation_id: int
    login: str
    account_id: int | None
    suspended: bool


def _covering(response: httpx.Response, owner: str) -> _Covering | None:
    """The installation on this page that covers the account, if one does.

    Every shape that is not the one expected answers None rather than raising. This runs on the
    way past a cache miss, and a body GitHub changed should cost an anonymous request rather than
    the whole call.
    """
    try:
        payload: object = response.json()
    except ValueError:
        return None
    if not is_json_list(payload):
        return None

    # `lower`, matching the store, rather than `casefold`. The two are not the same function and
    # the store keys every row on `account_login.strip().lower()`, so a match normalised the other
    # way could select a row that could not be read back: U+017F casefolds to `s` and lower()
    # leaves it alone, and the owner asked about is not always a login GitHub vouched for. Nothing
    # is lost, because GitHub logins are ASCII - which is exactly why the two agree on them.
    wanted = owner.strip().lower()
    for row in payload:
        if not is_json_object(row):
            continue
        account = row.get("account")
        if not is_json_object(account):
            # An installation on an enterprise rather than an account. GitHub sends `name` and
            # `slug` for those and no `login`, and an enterprise owns no repositories to mirror.
            continue
        login = account.get("login")
        if not isinstance(login, str) or login.strip().lower() != wanted:
            continue
        installation = row.get("id")
        if not isinstance(installation, int):
            continue
        account_id = account.get("id")
        return _Covering(
            installation_id=installation,
            login=login,
            # Kept where GitHub gave one, because the store says it is the only thing that
            # tells a rename apart from somebody taking a freed name.
            account_id=account_id if isinstance(account_id, int) else None,
            # A timestamp or null, and only its presence matters here.
            suspended=row.get("suspended_at") is not None,
        )
    return None


def _minted(response: httpx.Response) -> tuple[str, datetime]:
    """The token and its expiry, refusing a body that is missing either.

    Neither default is safe: an assumed hour caches a token that may already be dead, and an
    assumed expiry of now mints on every request. A body this shape means GitHub changed something.
    """
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise GitHubAuthError("GitHub returned a non-JSON installation token") from exc

    if not is_json_object(payload):
        raise GitHubAuthError("GitHub returned an installation token with no token or no expiry")
    token = payload.get("token")
    expires_at = parse_timestamp(payload.get("expires_at"))
    if not isinstance(token, str) or not token or expires_at is None:
        raise GitHubAuthError("GitHub returned an installation token with no token or no expiry")
    return token, expires_at
