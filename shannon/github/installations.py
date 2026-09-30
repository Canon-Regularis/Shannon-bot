"""Turning "which account is this call about" into a token that can see it.

The second half of App authentication: `app_auth` signs the JWT that proves this process is the
App, and this trades that JWT for a token scoped to one installation, kept until it expires.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.domain.json import is_json_list, is_json_object
from shannon.github.app_auth import app_jwt
from shannon.github.errors import GitHubAuthError
from shannon.github.mapping import parse_timestamp
from shannon.github.responses import json_object

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


class InstallationDirectory:
    """Owner to installation, out of the database.

    No in-process cache: the token cache in front absorbs the hot path, leaving one query an hour.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def installation_for(self, owner: str) -> MapSays:
        async with self._sessionmaker() as session:
            found = await InstallationStore(session).for_owner(owner)
        if found is None:
            return MapSays(Knows.NOTHING)
        if found.suspended:
            # Suspended is not uninstalled, and minting against a suspended installation
            # fails, so there is nothing to be gained by trying.
            logger.info("the installation for %s is suspended, so nothing can be read", owner)
            return MapSays(Knows.A_SUSPENSION)
        return MapSays(Knows.AN_INSTALLATION, found.installation_id)

    async def remember(
        self,
        *,
        installation_id: int,
        account_login: str,
        account_id: int | None,
        suspended: bool,
    ) -> None:
        """Write down an installation this bot had not been told about.

        `for_owner` has said since it was written that None means "ask GitHub" rather than
        "not installed". Nothing asked, and nothing could keep the answer if it had. This is
        the keeping half.
        """
        async with self._sessionmaker() as session, session.begin():
            await InstallationStore(session).remember(
                installation_id=installation_id,
                account_login=account_login,
                account_id=account_id,
                suspended=suspended,
            )

    async def forget(self, installation_id: int) -> None:
        """Drop a row GitHub will not honour, so the next call can learn what replaced it.

        A reinstall keeps the login and issues a NEW installation id, so a row holding the old one
        is worse than no row: it answers `AN_INSTALLATION`, the asking never happens, and every
        call posts a mint GitHub answers 404 - which caches nothing, because there is no token to
        cache. Forgetting it turns the next call into a question, and a question is the one thing
        this module can now answer.
        """
        async with self._sessionmaker() as session, session.begin():
            await InstallationStore(session).forget(installation_id)


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
        # because the owner is not always a login this bot vouched for (the `/set_board` owner
        # option is free text), so a per-owner map is a dictionary anybody can grow. Two
        # different uncached owners queue behind each other, which costs one HTTP call of
        # waiting on a path that only runs when the map has nothing.
        self._discovery = asyncio.Lock()
        # None until asked for, "" once asked and not answered: the second stops a failing
        # read being retried on every `/register`.
        self._slug: str | None = None

    def app_token(self) -> str:
        """The App's own JWT, for the handful of endpoints that take one rather than a token."""
        return app_jwt(
            client_id=self._client_id, private_key_pem=self._private_key_pem, now=self._now()
        )

    async def token_for(self, owner: str) -> str:
        # Signed once and carried down, rather than signed again by everything below. Each call
        # re-parses the PEM and performs an RSA signature on the event loop, and `_authorization`
        # asks for a token once per page of a paged read.
        jwt = self.app_token()
        if not jwt:
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
            installation = await self._discover(owner, jwt)
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
            return await self._mint(installation, owner, jwt)

    async def _discover(self, owner: str, jwt: str) -> int | None:
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

            found = await self._covering_installation(owner, jwt)
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

    async def _covering_installation(self, owner: str, jwt: str) -> _Covering | None:
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
        url: str | None = "/app/installations"
        params: dict[str, int] = {"per_page": 100}
        for _ in range(MAX_INSTALLATION_PAGES):
            if url is None:
                return None

            response = await self._http.get(
                url, params=params or None, headers={"Authorization": f"Bearer {jwt}"}
            )
            if response.status_code >= 400:
                # Logged rather than raised, unlike `installed_on`: nobody asked this question, it
                # is a repair attempted on the way past, and failing it should leave the caller
                # with the anonymous request it would have made anyway rather than an exception.
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

    async def installed_on(self, owner: str, name: str) -> int | None:
        """Which installation covers one repository, asked of GitHub rather than of the cache.

        A repository nobody has registered has no row, and no row means ask GitHub rather than
        not installed. A JWT, because there is no token until this has answered. GitHub 404s both
        for a repository that does not exist and for one the App was never installed on, and
        separating the two would say whether a private repository exists.
        """
        if not self.app_token():
            return None

        response = await self._http.get(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/installation",
            headers={"Authorization": f"Bearer {self.app_token()}"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubAuthError(
                f"GitHub refused to say whether the app is installed on {owner}/{name} "
                f"({response.status_code}). Check the GitHub App's id and private key."
            )

        payload = json_object(response)
        installation = payload.get("id")
        return installation if isinstance(installation, int) else None

    async def app_slug(self) -> str:
        """The App's own slug, for building the "install me" link, read once and kept.

        Read from GitHub rather than configured: it never changes for a given App.
        """
        if self._slug is not None:
            return self._slug
        if not self.app_token():
            return ""

        response = await self._http.get(
            "/app", headers={"Authorization": f"Bearer {self.app_token()}"}
        )
        if response.status_code >= 400:
            # Not raised: the slug only makes a message more helpful, and `/register` should
            # not fail because the link could not be prettified.
            logger.warning("could not read the app's own slug (%s)", response.status_code)
            return ""

        slug = json_object(response).get("slug")
        self._slug = slug if isinstance(slug, str) else ""
        return self._slug

    async def _mint(self, installation: int, owner: str, jwt: str) -> str:
        """Ask GitHub for a token, or answer "" for an installation that has gone.

        A 404 means the installation was removed between the directory read and this call, which
        is permanent and the same outcome as never having been installed. Everything else raises:
        a 401 is a key GitHub will not accept, and calling that "no installation" misdirects.
        """
        response = await self._http.post(
            f"/app/installations/{installation}/access_tokens",
            headers={"Authorization": f"Bearer {jwt}"},
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
            raise GitHubAuthError(
                f"GitHub refused an installation token for {owner} "
                f"({response.status_code}). Check the GitHub App's id and private key."
            )

        token, expires_at = _minted(response)
        self._minted[installation] = (token, expires_at)
        return token


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
        payload: Any = response.json()
    except ValueError as exc:
        raise GitHubAuthError("GitHub returned a non-JSON installation token") from exc

    token = payload.get("token") if is_json_object(payload) else None
    expires_at = parse_timestamp(payload.get("expires_at")) if is_json_object(payload) else None
    if not isinstance(token, str) or not token or expires_at is None:
        raise GitHubAuthError("GitHub returned an installation token with no token or no expiry")
    return token, expires_at
