from __future__ import annotations

from shannon.domain.models import PullRequestSnapshot, RepositorySnapshot
from shannon.github.errors import GitHubNotFoundError


class FakeGitHubClient:
    """GitHubClient backed by dictionaries, for tests that must not touch the network."""

    def __init__(
        self,
        *,
        repositories: dict[str, RepositorySnapshot] | None = None,
        pull_requests: dict[tuple[str, int], PullRequestSnapshot] | None = None,
    ) -> None:
        self.repositories = repositories or {}
        self.pull_requests = pull_requests or {}
        self.repository_calls: list[str] = []
        self.pull_request_calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        full_name = f"{owner}/{name}"
        self.repository_calls.append(full_name)
        if self.error is not None:
            raise self.error
        try:
            return self.repositories[full_name.lower()]
        except KeyError:
            raise GitHubNotFoundError(f"GitHub has nothing at /repos/{full_name}") from None

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        key = (f"{owner}/{name}".lower(), number)
        self.pull_request_calls.append(key)
        if self.error is not None:
            raise self.error
        try:
            return self.pull_requests[key]
        except KeyError:
            raise GitHubNotFoundError(
                f"GitHub has nothing at /repos/{owner}/{name}/pulls/{number}"
            ) from None
