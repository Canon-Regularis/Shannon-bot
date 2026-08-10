class ShannonError(Exception):
    """Base for every error this project raises deliberately."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnparseableLinkError(ShannonError):
    """A GitHub link did not match the shape the parser expects."""


class NotRegisteredError(ShannonError):
    """The guild has no repository bound to it yet."""


class DuplicateRegistrationError(ShannonError):
    """The guild already has a repository, or the repository is bound elsewhere."""


class RepositoryMismatchError(ShannonError):
    """The link points at a repository other than the one registered here."""
