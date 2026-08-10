from enum import StrEnum


class ObjectType(StrEnum):
    """What a tracked item points at on GitHub."""

    PR = "PR"
    ISSUE = "ISSUE"
    TICKET = "TICKET"


class Status(StrEnum):
    NOT_REVIEWED = "NOT_REVIEWED"
    IN_REVIEW = "IN_REVIEW"
    READY_FOR_MERGE = "READY_FOR_MERGE"
    BACKLOG = "BACKLOG"
    DONE = "DONE"


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNSET = "UNSET"


class ActorRole(StrEnum):
    """How a GitHub user relates to a tracked item."""

    AUTHOR = "AUTHOR"
    ASSIGNEE = "ASSIGNEE"
    REVIEWER = "REVIEWER"
    PROJECT_MANAGER = "PROJECT_MANAGER"


class CommandRole(StrEnum):
    """Permission tiers a Discord member can hold."""

    ADMIN = "ADMIN"
    PROJECT_MANAGER = "PROJECT_MANAGER"
    REVIEWER = "REVIEWER"
    DEVELOPER = "DEVELOPER"
