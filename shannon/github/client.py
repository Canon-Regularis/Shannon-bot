from __future__ import annotations

import contextlib
import logging
import time
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

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
