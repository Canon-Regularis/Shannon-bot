class ShannonError(Exception):
    """Base for every error this project raises deliberately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PermanentError(ShannonError):
    """Something a retry cannot fix.

    The worker retries a failed handler for roughly two hours; anything raised as this is recorded
    and dropped on the first attempt.
    """


class UnparseableLinkError(ShannonError):
    """A GitHub link did not match the shape the parser expects."""


class NotRegisteredError(ShannonError):
    """The guild has no repository bound to it yet."""


class DuplicateRegistrationError(ShannonError):
    """The guild already has a repository, or the repository is bound elsewhere."""


class NotInstalledError(ShannonError):
    """This bot cannot see a repository: the GitHub App is not installed on it, or the
    installation is suspended (#268).

    GitHub answers 404 both for a repository that does not exist and for one the caller may not
    see, so a plain `GitHubNotFoundError` reports a private repository as missing.
    """


class NotProvenError(ShannonError):
    """The caller has not shown that GitHub agrees they may do this.

    A permission denial means the server has not given you the role; this means GitHub has not
    given you the repository.
    """


class RepositoryMismatchError(ShannonError):
    """The link points at a repository other than the one registered here."""


class ItemNotReadyError(ShannonError):
    """The item is tracked but its Discord thread does not exist yet.

    Deliberately not permanent: the sync that opens the thread is in flight or on its own backoff.
    """
