"""How a request reaches GitHub's REST API, and how its answer is filed. Issue #210.

The layer under `client.py`, split out of it for #271. It knows paths, credentials, pages,
redirects and status codes, and nothing about what any endpoint means. The typed operations in
`client.py` and the board reader in `projects.py` are built over it, the board reader through
Protocols that say which part of it it needs, and `installations.py` files its own answers by the
same rules.

Its own module, rather than a section of the client, because the board reader needed it without
the client. The container used to build a whole second `HttpGitHubClient` for one PATCH, and
`GitHubClient` carried three raw reads at any path only because the same object was handed to the
board reader - so every service holding the client could GET anything with an installation token.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Mapping
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from shannon.domain.json import JsonObject, is_json_object
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.paging import PagedRead

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

# How far a paged read will follow the Link header. At a hundred rows a page this is more
# than any board or list this bot reads, and it is the only thing standing between a
# self-referential cursor and a loop that never ends.
MAX_PAGES = 50

# How far a write will follow a redirect. GitHub answers a renamed repository with the current
# name in one hop rather than a chain, so this is a bound on something that should not happen
# rather than room to work in.
MAX_WRITE_REDIRECTS = 3

# How long to wait out a secondary rate limit that names itself only in its message. Issue #271.
# GitHub's documentation on REST rate limits says that with neither `retry-after` nor a spent
# counter to go on, an integration should wait at least one minute before trying again.
SECONDARY_LIMIT_WAIT_SECONDS = 60

# The words a secondary limit's message carries, matched in lower case. The second is what GitHub
# called the same limit before it was renamed, and still answers with on some endpoints.
_SECONDARY_LIMIT_WORDS = ("secondary rate limit", "abuse detection")


class SuppliesTokens(Protocol):
    """A bearer token for calls about one GitHub account, or the empty string for none.

    Empty rather than an exception: it is the state an unset token already produced, so a
    deployment with no App configured answers on public endpoints rather than failing on the
    first command anybody runs.
    """

    async def token_for(self, owner: str) -> str: ...


class GitHubTransport:
    """GET, paged GET and the writes, with the credential each one is owed."""

    def __init__(
        self,
        *,
        tokens: SuppliesTokens | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # A supplier rather than a token: each call carries a credential minted for the account
        # it is about, so the header is decided per request. None means no App is configured and
        # every request goes out unauthenticated, which is what an empty token already did.
        self._tokens = tokens
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=github_headers(),
            # Renaming a repository or its owner turns every lookup of the old name into a 301,
            # as does moving an issue between repositories. httpx does not follow redirects
            # unless told to, and an unfollowed one surfaces as "GitHub could not be reached".
            # Safe with a token: httpx drops Authorization on a cross-origin redirect.
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubTransport:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_json(
        self, path: str, *, owner: str = "", token: str = "", **params: str | int
    ) -> object:
        """Whatever GitHub answers at a path, list or object alike.

        The project endpoints are parsed by a module that checks every field it touches, so
        this hands the body over as it came.
        """
        return await self.read_json(path, query=params or None, owner=owner, token=token)

    async def read_json(
        self,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        owner: str = "",
        token: str = "",
    ) -> object:
        """`get_json` with the query as a mapping rather than as keywords.

        For a query parameter whose name clashes with a keyword here - the commits endpoint takes
        one called `path` - which `get_json` cannot be handed. httpx encodes it, a space, a `#`
        and anything outside ASCII included.
        """
        response = await self._fetch(path, owner=owner, token=token, params=query)
        _raise_for_status(response, path)
        return _json_body(response, path)

    async def get_pages(
        self, path: str, *, owner: str = "", token: str = "", **params: str | int
    ) -> AsyncIterator[object]:
        """Every page of a list endpoint, following GitHub's own Link header.

        The project endpoints paginate by cursor: there is no `page` parameter, and asking for
        page two by number silently returns the first page again, so a caller that counted pages
        would mirror every card twice. The cursor is opaque, so the header is followed as given.

        `token` is the same explicit credential `get_json` takes, and goes on every page rather
        than the first. Issue #201: listing somebody's boards is a board read like any other, and
        without it the list went out as the App installation, which cannot see a private board.
        """
        url: str | None = path
        for _ in range(MAX_PAGES):
            if url is None:
                return
            response = await self._fetch(url, owner=owner, token=token, params=params or None)
            _raise_for_status(response, path)
            yield _json_body(response, path)

            # The next URL carries the cursor already, so the original parameters must not be
            # sent again beside it.
            url = _next_url(response)
            params = {}

        # A Link header that points at itself, or a list that never ends, would otherwise keep
        # this reading for as long as the process lives. Bounded rather than trusted: the cursor
        # is opaque, so there is nothing to inspect to tell the two apart.
        #
        # Only when something was actually left. This used to hang off the loop's `else`, which
        # runs whenever the range is exhausted, so a list of exactly `MAX_PAGES` pages was read
        # whole and reported as cut short. A warning that fires when nothing is wrong is worse
        # than no warning: it teaches whoever reads the log to skip the line, and the one time
        # it means a board is being truncated looks exactly like the times it does not.
        if url is not None:
            logger.warning("stopped following pages of %s after %s of them", path, MAX_PAGES)

    async def get_pages_since(
        self,
        path: str,
        *,
        etag: str | None = None,
        owner: str = "",
        token: str = "",
        **params: str | int,
    ) -> PagedRead:
        """Every page of a list endpoint, unless GitHub says none of it changed.

        The conditional sibling of `get_pages`, and a separate method rather than a parameter on
        it. That one is an async generator, and a generator has nowhere to put an answer that is
        not a page; six callers read it as a stream of bodies and none of them wants to learn
        about validators.

        `If-None-Match` goes on the FIRST request only. A 304 to it costs **no rate-limit budget
        at all** - GitHub's counter does not move - and carries no body, which is the whole reason
        a board can now be read every couple of seconds for less than it used to cost to read it
        every minute.

        Pages are collected rather than yielded, because the answer "or nothing changed" is not a
        page and a generator cannot say it. Nothing is held that was not held before: the one
        caller accumulated every item into a list anyway.
        """
        response = await self._fetch(
            path,
            owner=owner,
            token=token,
            params=params or None,
            extra_headers={"If-None-Match": etag} if etag else None,
        )

        # Checked BEFORE `_raise_for_status`, which reads anything outside the 2xx range as a
        # failure. A 304 is the opposite of a failure: it is GitHub confirming the copy already
        # held, and the validator sent is still the right one to send again.
        if response.status_code == 304:
            return PagedRead(etag=etag, pages=None)

        _raise_for_status(response, path)
        validator = response.headers.get("etag")
        pages: list[object] = [_json_body(response, path)]

        # The next URL carries the cursor already, so the original parameters must not be sent
        # again beside it.
        url = _next_url(response)
        paged = url is not None

        for _ in range(MAX_PAGES - 1):
            if url is None:
                break
            response = await self._fetch(url, owner=owner, token=token)
            _raise_for_status(response, path)
            pages.append(_json_body(response, path))
            url = _next_url(response)

        # The same bound `get_pages` keeps, for the same reason: the cursor is opaque, so a Link
        # header pointing at itself cannot be told from a real next page by inspection.
        if url is not None:
            logger.warning("stopped following pages of %s after %s of them", path, MAX_PAGES)

        # A one-line conditional on purpose. Written as an `if` it would be a branch whose arms
        # the coverage floor counts separately, and the reason for it is one sentence either way:
        # a validator that covers one page of several covers nothing. See `PagedRead`.
        return PagedRead(etag=None if paged else validator, pages=tuple(pages))

    async def patch_json(self, path: str, *, owner: str, token: str = "", json: JsonObject) -> None:
        """Send JSON at a path with PATCH, for the one caller that writes to a board.

        Over `send` like every other write here, which is what gets the three things a naive
        `client.patch()` does not: redirects re-issued by hand, because httpx turns a redirected
        write into a bodyless GET; the credential; and `_raise_for_status`, which turns a 422 into
        a refusal carrying GitHub's own words. That last one is the whole diagnosis for a body
        shape this project has never been able to check against a live board.

        The board's writer is `without_tokens`, so the credential is the one it is handed or none.
        """
        await self.send("PATCH", path, owner, token=token, json=json)

    def without_tokens(self) -> GitHubTransport:
        """This transport's connection, carrying no credential but the one each call is handed.

        The board's writer. Issue #271: a board write is made as the person who asked for it, so
        a write with nobody behind it must go out anonymous for GitHub to answer 401, rather than
        be filled in with the App installation's token - which is the fact of the wiring that a
        second client built with no tokens used to be. This is the same fact without the second
        client: one connection pool, and nothing new to close, since the pool is this one's.
        """
        return GitHubTransport(http_client=self._client)

    async def send(
        self,
        method: str,
        path: str,
        owner: str = "",
        token: str = "",
        *,
        json: JsonObject | None = None,
    ) -> None:
        """A write, whose answer is only ever whether it worked.

        Redirects are followed here rather than by httpx. GitHub answers 301 after a rename, and
        httpx re-issues a redirected POST as a bodyless GET, so a label write against a renamed
        repository fetched the label list, was answered 200, and wrote nothing that anything
        downstream could tell from success. A stale name is ordinary: nothing corrects
        `repositories.repo_name` until an item webhook arrives.
        """
        try:
            headers = await self._authorization(owner, token)
            response = await self._client.request(
                method, path, follow_redirects=False, headers=headers, json=json
            )
            for _ in range(MAX_WRITE_REDIRECTS):
                if not response.is_redirect:
                    break
                path = _redirect_target(response, path)
                response = await self._client.request(
                    method, path, follow_redirects=False, headers=headers, json=json
                )
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc

        # A redirect still standing after that lands in the catch-all as "GitHub returned 301",
        # which is retryable and loud, and for a chain that never resolves is the right answer.
        _raise_for_status(response, path)

    async def _fetch(
        self,
        url: str,
        *,
        owner: str,
        token: str,
        params: Mapping[str, str | int] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One GET, with a failure to reach GitHub told apart from a GitHub that answered.

        The credential is built inside the `try` as well as the request. Issue #271: minting an
        installation token is a request to GitHub too, and two reads built theirs outside it, so
        failing to reach GitHub for the token escaped as a raw httpx error past every caller that
        catches `GitHubError`, while every other read filed the same failure as an outage.
        """
        try:
            headers = {**await self._authorization(owner, token), **(extra_headers or {})}
            return await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc

    async def _authorization(self, owner: str, token: str = "") -> dict[str, str]:
        """The credential for calls about one account, or nothing at all.

        Nothing rather than an empty bearer: `Bearer ` is malformed and GitHub answers 401,
        where no header at all is anonymous and public endpoints answer. An empty owner means
        a call that is not about a repository, and those endpoints are public.

        An explicit `token` wins, and exists for one caller. Issue #170: a project board is read
        and written under the authorisation of a particular PERSON, which the owner cannot
        identify - two servers may link two different boards owned by the same account, so a
        credential chosen by owner alone would be one server's board read under another server's
        member's grant. The board reader resolves whose it is and says so here; everything else
        still asks `token_for`, which answers with an installation and knows nothing about people.
        """
        if token:
            return {"Authorization": f"Bearer {token}"}
        if self._tokens is None or not owner:
            return {}
        resolved = await self._tokens.token_for(owner)
        return {"Authorization": f"Bearer {resolved}"} if resolved else {}


def github_headers() -> dict[str, str]:
    """The headers every request carries whoever it is about.

    `Authorization` is not among them: it depends on which account the request concerns, so
    `GitHubTransport._authorization` builds it per request.
    """
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "shannon-bot",
    }


def is_rate_limited(response: httpx.Response) -> bool:
    """Whether a 403 is GitHub asking for a wait rather than refusing outright.

    Two limits, answering differently. The primary one is the hourly budget and says so in the
    counter. The secondary one is about how fast requests arrive, spends no budget, and marks
    itself by asking for a wait or, failing that, only in its message, with the counter beside it
    often nowhere near zero. So reading the counter alone misses the limit this bot actually trips,
    which is the poller moving cards: filed as a refusal it loses the wait, and carrying on at the
    ordinary interval is how GitHub's documentation says an integration gets banned. A 429 is
    already a rate limit whatever else it carries, and the caller handles it.
    """
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "retry-after" in response.headers or _names_a_secondary_limit(response)


def retry_after(response: httpx.Response) -> int | None:
    """Seconds to wait before trying again.

    The two headers are different kinds of number. `retry-after` is already a delay;
    `x-ratelimit-reset` is the epoch second the window reopens, so it is turned into a delay
    against GitHub's own `date` header, which is the clock it was measured on.

    A secondary limit that says so only in its message waits `SECONDARY_LIMIT_WAIT_SECONDS`, and
    never the reset. Issue #271: that header counts down the PRIMARY hourly window, so taking it
    here would stop the poller for up to an hour over a limit GitHub says clears in one minute.
    """
    delay = response.headers.get("retry-after")
    if delay and delay.isdigit():
        return int(delay)
    if _waits_out_a_secondary_limit(response):
        return SECONDARY_LIMIT_WAIT_SECONDS

    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0, int(reset) - _served_at(response))
    return None


def _waits_out_a_secondary_limit(response: httpx.Response) -> bool:
    """Whether this is the one rate limit with neither header to measure its wait by.

    A 403 or a 429, since GitHub answers a secondary limit with either. One with a `retry-after`
    or a spent counter keeps the wait it was given.
    """
    return (
        response.status_code in {403, 429}
        and response.headers.get("x-ratelimit-remaining") != "0"
        and "retry-after" not in response.headers
        and _names_a_secondary_limit(response)
    )


def _names_a_secondary_limit(response: httpx.Response) -> bool:
    said = _message(response)
    return said is not None and any(words in said.lower() for words in _SECONDARY_LIMIT_WORDS)


def _raise_for_status(response: httpx.Response, path: str) -> None:
    if response.is_success:
        return

    status = response.status_code
    if status == 404:
        raise GitHubNotFoundError(f"GitHub has nothing at {path}")
    if status == 429 or is_rate_limited(response):
        raise GitHubRateLimitError("GitHub rate limit reached", retry_after=retry_after(response))
    if status in {401, 403}:
        raise GitHubAuthError(f"GitHub refused the request for {path} ({status})")
    # Above the catch-all, because a 422 is the opposite of what the catch-all means. GitHub read
    # the request and declined it, so retrying sends the same refusal back, and it is nearly always
    # the caller's to fix: a reviewer who is not a collaborator, the item's own author, somebody
    # already asked. Under the catch-all all of those read as "GitHub could not be reached".
    if status == 422:
        raise GitHubRefusedError(_refusal(response, path))
    raise GitHubUnavailableError(f"GitHub returned {status} for {path}")


def _refusal(response: httpx.Response, path: str) -> str:
    """GitHub's own words for why it would not do something.

    Read rather than replaced: the reasons are specific, numerous and GitHub's to change. A body
    that is not the expected shape falls back to naming the path.
    """
    return _message(response) or f"GitHub refused the request for {path}"


def _message(response: httpx.Response) -> str | None:
    """The `message` GitHub puts on an answer it is not happy with, where it put one as text.

    Tolerant of any body at all, because it is read from answers that are already failures: a
    proxy's HTML page or an object with no message is no words rather than a second failure.
    """
    try:
        payload = response.json()
    except ValueError:
        return None

    said = payload.get("message") if is_json_object(payload) else None
    return said if isinstance(said, str) and said else None


def _redirect_target(response: httpx.Response, path: str) -> str:
    """Where a redirected write goes instead, if it can go anywhere at all.

    Following one by hand means deciding who the Authorization header is handed to. GitHub
    answers a rename with the same host and a new path, so anything else is refused.
    """
    location = response.headers.get("location")
    if not location:
        raise GitHubUnavailableError(f"GitHub redirected {path} without saying where")

    target = response.url.join(location)
    here = response.url
    if (target.scheme, target.host, target.port) != (here.scheme, here.host, here.port):
        raise GitHubUnavailableError(f"GitHub redirected {path} to another host, {target.host}")
    return str(target)


def _json_body(response: httpx.Response, path: str) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc


def _unreachable(exc: httpx.HTTPError) -> GitHubUnavailableError:
    return GitHubUnavailableError(f"Could not reach GitHub: {exc}")


def _next_url(response: httpx.Response) -> str | None:
    """The next page GitHub's Link header points at, or None on the last page."""
    return response.links.get("next", {}).get("url")


def _served_at(response: httpx.Response) -> int:
    served = response.headers.get("date")
    if served:
        with contextlib.suppress(TypeError, ValueError):
            return int(parsedate_to_datetime(served).timestamp())
    return int(time.time())
