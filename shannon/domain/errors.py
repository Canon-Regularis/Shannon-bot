class ShannonError(Exception):
    """Base for every error this project raises deliberately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PermanentError(ShannonError):
    """Something a retry cannot fix.

    The worker retries a failed handler for roughly two hours, which suits Discord being briefly
    unreachable and does nothing for a missing permission. Anything raised as this is recorded
    and dropped on the first attempt.
    """


class UnparseableLinkError(ShannonError):
    """A GitHub link did not match the shape the parser expects."""


class NotRegisteredError(ShannonError):
    """The guild has no repository bound to it yet."""


class DuplicateRegistrationError(ShannonError):
    """The guild already has a repository, or the repository is bound elsewhere."""


class NotInstalledError(ShannonError):
    """This bot cannot see a repository because the GitHub App is not installed on it.

    Its own error rather than a `GitHubNotFoundError`, and that distinction is the whole of what
    issue #98 was about. GitHub answers 404 both for a repository that does not exist and for one
    the caller may not see, so a private repository used to be reported as missing: the reply said
    it could not be found, and the person reading it went and checked the spelling of a link that
    was perfectly correct.

    The message carries what to do about it, because unlike the 404 it replaces there is
    something to do.
    """


class NotProvenError(ShannonError):
    """The caller has not shown that GitHub agrees they may do this.

    Raised where a Discord role is not enough, which today is `/unregister` alone. Its own error
    rather than a permission denial, because the two say different things: a denial means the
    server has not given you the role, and this means GitHub has not given you the repository.
    """


class RepositoryMismatchError(ShannonError):
    """The link points at a repository other than the one registered here."""


class ItemNotReadyError(ShannonError):
    """The item is tracked but its Discord thread does not exist yet.

    Deliberately not permanent. The sync that opens the thread is either in flight or waiting on
    its own backoff, and the note belongs in that thread once it is there.
    """


class WrongPolicyError(PermanentError):
    """A sync policy was handed a snapshot of a kind it does not handle.

    A wiring mistake rather than anything a GitHub payload can cause, so retrying it for two
    hours would only delay the traceback that explains it.
    """
