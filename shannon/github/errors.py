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

    Its own type rather than falling in with the unavailable one, because the two want opposite
    things. A GitHub that could not be reached is worth trying again; a refusal never is, and it is
    almost always something the person who asked can put right themselves: a reviewer who is not a
    collaborator on the repository, the pull request's own author, somebody who was already asked.

    The message is GitHub's own. It names which of those it was, and any sentence written here
    would be a worse guess kept in step with GitHub's list by hand.
    """


class GitHubUnavailableError(GitHubError):
    """GitHub returned a server error or the request never completed."""
