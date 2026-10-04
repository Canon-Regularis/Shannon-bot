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
    """This bot cannot see a repository because the GitHub App is not installed on it.

    GitHub answers 404 both for a repository that does not exist and for one the caller may not
    see, so a plain `GitHubNotFoundError` reports a private repository as missing.
    """


class NotProvenError(ShannonError):
    """The caller has not shown that GitHub agrees they may do this.

    A permission denial means the server has not given you the role; this means GitHub has not
    given you the repository.
    """


class BoardNotAuthorisedError(ShannonError):
    """Nobody has given this bot a GitHub authorisation it could use to reach the board.

    Issue #170. Three refusals about a board read almost alike and send somebody to three
    different places, so they are three types:

    - `NotProvenError` above: GitHub has not said this account is yours.
    - `BoardUnreadableError`: there is an authorisation, and GitHub will not open that board with
      it - a wrong number, a wrong owner, or a grant that does not cover it.
    - this one: there is no authorisation at all, which is the only one of the three the person
      reading the message can fix in one command, by themselves.

    Here rather than beside the other board errors in `services.boards`, because the commands and
    the workflow both raise it and a service importing another service for an exception is the
    coupling issue #170 set out to reduce.
    """


class RepositoryMismatchError(ShannonError):
    """The link points at a repository other than the one registered here."""


class ItemNotReadyError(ShannonError):
    """The item is tracked but its Discord thread does not exist yet.

    Deliberately not permanent: the sync that opens the thread is in flight or on its own backoff.
    """


class WrongPolicyError(PermanentError):
    """A sync policy was handed a snapshot of a kind it does not handle.

    A wiring mistake rather than anything a GitHub payload can cause, so retrying it for two hours
    would only delay the traceback that explains it.
    """
