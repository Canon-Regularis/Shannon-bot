class ShannonError(Exception):
    """Base for every error this project raises deliberately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PermanentError(ShannonError):
    """Something a retry cannot fix.

    The delivery worker retries a failed handler ten times over roughly two hours, which is the
    right answer for Discord being briefly unreachable and the wrong one for a missing
    permission. Anything raised as this is recorded and dropped on the first attempt.
    """


class UnparseableLinkError(ShannonError):
    """A GitHub link did not match the shape the parser expects."""


class NotRegisteredError(ShannonError):
    """The guild has no repository bound to it yet."""


class DuplicateRegistrationError(ShannonError):
    """The guild already has a repository, or the repository is bound elsewhere."""


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
