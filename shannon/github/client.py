from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from shannon.domain.models import IssueSnapshot, PullRequestSnapshot, RepositorySnapshot
from shannon.github import mapping
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)

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


class LooksUpRepository(Protocol):
    """Resolving a repository by owner and name.

    Split out because that is all the link commands need of GitHub directly. Fetching the item
    itself is a closure the wiring builds, so nothing has to hold a handle that can read every
    pull request in order to check one repository still exists.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...


class GitHubClient(LooksUpRepository, Protocol):
    """The GitHub calls the rest of the project is allowed to make.

    Commands and services depend on this rather than on httpx, so nothing outside this module
    knows GitHub is reached over HTTP.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot: ...

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    # Untyped bodies, for the project endpoints, which answer with arrays and are parsed by a
    # module that checks every field it touches. Declared here because the wiring hands this
    # same object to the board reader, and a stand-in that satisfied the protocol without them
    # would build a container that fails on the first poll rather than at the seam.
    async def get_json(self, path: str, **params: Any) -> Any: ...

    def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]: ...


class HttpGitHubClient:
    def __init__(
        self,
        *,
        token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=_headers(token),
            # Renaming a repository or its owner turns every lookup of the old name into a 301,
            # as does moving an issue between repositories. httpx does not follow redirects
            # unless told to, and an unfollowed one surfaces as "GitHub could not be reached".
            # Safe with a token: httpx drops Authorization on a cross-origin redirect.
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpGitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        payload = await self._get(f"/repos/{owner}/{name}")
        snapshot = mapping.repository(payload)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable repository for {owner}/{name}"
            )
        return snapshot

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        payload = await self._get(f"/repos/{owner}/{name}/pulls/{number}")

        # The PR response embeds its own repository under base.repo, which saves a second call.
        base = payload.get("base") if isinstance(payload, dict) else None
        repo = mapping.repository(base.get("repo") if isinstance(base, dict) else None)
        if repo is None:
            repo = await self.get_repository(owner, name)

        snapshot = mapping.pull_request(payload, repo)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable pull request for {owner}/{name}#{number}"
            )
        return snapshot

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot:
        payload = await self._get(f"/repos/{owner}/{name}/issues/{number}")

        # GitHub serves pull requests from this endpoint as well, so a number that turns out to
        # be a pull request is reported as no such issue rather than tracked as one.
        if mapping.is_pull_request(payload):
            raise GitHubNotFoundError(f"{owner}/{name}#{number} is a pull request, not an issue")

        # Unlike the pull request endpoint, this one carries no repository object, only a URL.
        repo = mapping.repository(payload.get("repository")) or await self.get_repository(
            owner, name
        )

        snapshot = mapping.issue(payload, repo)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable issue for {owner}/{name}#{number}"
            )
        return snapshot

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Put a label on an item.

        The issues endpoint serves pull requests too, so one method covers both. GitHub creates
        a label this repository does not have yet rather than refusing, which is what lets a
        server start using the workflow without setting five labels up by hand first.
        """
        await self._send(
            "POST", f"/repos/{owner}/{name}/issues/{number}/labels", json={"labels": [label]}
        )

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Take a label off an item, treating one that is not there as done.

        Removals are computed from a snapshot read a moment earlier, and anything can have
        happened since. A 404 here means the end state is the wanted one, and failing the
        command over it would leave the caller retrying towards where they already are.
        """
        path = f"/repos/{owner}/{name}/issues/{number}/labels/{quote(label, safe='')}"
        with contextlib.suppress(GitHubNotFoundError):
            await self._send("DELETE", path)

    async def _send(self, method: str, path: str, **kwargs: Any) -> None:
        """A write, whose answer is only ever whether it worked.

        Redirects are followed here rather than by the transport, because httpx follows one the
        way the RFC allows and not the way a write needs. A 301 is what GitHub answers after a
        rename, and on a POST httpx re-issues it as a bodyless GET, so putting a label on a
        renamed repository fetched the label list, was answered 200, and wrote nothing. Nothing
        downstream can tell that from success: the command replies that it worked, the status
        goes into the row and into the thread, and no later delivery re-derives status from
        labels, so the item keeps the label it had. The DELETE beside it is not downgraded and
        does land, which leaves the item with its old status label stripped and no new one.

        The stale name is ordinary rather than rare. Nothing corrects `repositories.repo_name`
        until an item webhook arrives, and no `repository` event is registered at all.
        """
        try:
            response = await self._client.request(method, path, follow_redirects=False, **kwargs)
            for _ in range(MAX_WRITE_REDIRECTS):
                if not response.is_redirect:
                    break
                path = _redirect_target(response, path)
                response = await self._client.request(
                    method, path, follow_redirects=False, **kwargs
                )
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        # A redirect still standing after that lands in the catch-all as "GitHub returned 301",
        # which is retryable and loud. That is the answer the write path had before this, and
        # for a chain that never resolves it is still the right one.
        _raise_for_status(response, path)

    async def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]:
        """Every page of a list endpoint, following GitHub's own Link header.

        The project endpoints paginate by cursor rather than by page number: there is no `page`
        parameter, and the cursor for the next page is only ever given in the Link header. Asking
        for page two by number is not an error, it is silently the first page again, so a caller
        that counted pages would read the same cards over and over and mirror each of them twice.

        Following the header rather than building the next URL, because the cursor is opaque and
        the shape of it is GitHub's business.
        """
        url: str | None = path
        for _ in range(MAX_PAGES):
            if url is None:
                return
            try:
                response = await self._client.get(url, params=params or None)
            except httpx.HTTPError as exc:
                raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

            _raise_for_status(response, path)
            try:
                yield response.json()
            except ValueError as exc:
                raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

            # The next URL carries the cursor already, so the original parameters must not be
            # sent again beside it.
            url = response.links.get("next", {}).get("url")
            params = {}
        else:
            # A Link header that points at itself, or a list that never ends, would otherwise
            # keep this reading for as long as the process lives. Bounded rather than trusted:
            # the cursor is opaque, so there is nothing to inspect to tell the two apart.
            logger.warning("stopped following pages of %s after %s of them", path, MAX_PAGES)

    async def get_json(self, path: str, **params: Any) -> Any:
        """Whatever GitHub answers at a path, list or object alike.

        The typed readers above each know what they asked for and refuse anything else. The
        project endpoints answer with arrays and are parsed by a module that checks every field
        it touches, so this hands the body over as it came and leaves the judging to them.
        """
        try:
            response = await self._client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        _raise_for_status(response, path)

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        _raise_for_status(response, path)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

        if not isinstance(payload, dict):
            raise GitHubUnavailableError(f"GitHub returned an unexpected body for {path}")
        return payload


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "shannon-bot",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _redirect_target(response: httpx.Response, path: str) -> str:
    """Where a redirected write goes instead, if it can go anywhere at all.

    Following one by hand means deciding for oneself who the Authorization header is handed to,
    which is the job httpx was doing. GitHub answers a rename with the same host and a new path,
    so anything else is refused rather than trusted with the token.
    """
    location = response.headers.get("location")
    if not location:
        raise GitHubUnavailableError(f"GitHub redirected {path} without saying where")

    target = response.url.join(location)
    here = response.url
    if (target.scheme, target.host, target.port) != (here.scheme, here.host, here.port):
        raise GitHubUnavailableError(f"GitHub redirected {path} to another host, {target.host}")
    return str(target)


def _raise_for_status(response: httpx.Response, path: str) -> None:
    if response.is_success:
        return

    status = response.status_code
    if status == 404:
        raise GitHubNotFoundError(f"GitHub has nothing at {path}")
    if status == 429 or _is_rate_limited(response):
        raise GitHubRateLimitError("GitHub rate limit reached", retry_after=_retry_after(response))
    if status in {401, 403}:
        raise GitHubAuthError(f"GitHub refused the request for {path} ({status})")
    raise GitHubUnavailableError(f"GitHub returned {status} for {path}")


def _is_rate_limited(response: httpx.Response) -> bool:
    # A spent rate limit arrives as 403, indistinguishable from a permission problem except
    # for this header.
    return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"


def _retry_after(response: httpx.Response) -> int | None:
    """Seconds to wait before trying again.

    The two headers GitHub can answer with are not the same kind of number. `retry-after` is
    already a delay; `x-ratelimit-reset` is the epoch second the window reopens, so returning
    it unchanged would report a wait of about fifty-six years. It is turned into a delay
    against GitHub's own `date` header, which is the clock it was measured on.
    """
    delay = response.headers.get("retry-after")
    if delay and delay.isdigit():
        return int(delay)

    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0, int(reset) - _served_at(response))
    return None


def _served_at(response: httpx.Response) -> int:
    served = response.headers.get("date")
    if served:
        with contextlib.suppress(TypeError, ValueError):
            return int(parsedate_to_datetime(served).timestamp())
    return int(time.time())
