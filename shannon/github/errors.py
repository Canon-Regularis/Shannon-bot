from __future__ import annotations

from shannon.domain.errors import ShannonError


class GitHubError(ShannonError):
    """Anything that went wrong talking to GitHub."""


class GitHubNotFoundError(GitHubError):
    """The repository or pull request does not exist, or the token cannot see it."""


class GitHubAuthError(GitHubError):
    """The token is missing, expired, or lacks the scope for this call."""


class GitHubRateLimitError(GitHubError):
    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GitHubRefusedError(GitHubError):
    """GitHub understood the request perfectly well and would not do it.

    Never worth retrying, unlike a GitHub that could not be reached: a reviewer who is not a
    collaborator, the author themselves, somebody already asked. The message is GitHub's own.
    """


class GitHubUnavailableError(GitHubError):
    """GitHub returned a server error or the request never completed."""
