from __future__ import annotations

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


class StateChange(StrEnum):
    """What a delivery did to an item, for the three moves worth saying out loud in a thread.

    Not the states an item can be in, which is what `display_state` answers: REOPENED is a move
    into `open` with no state of its own, and CLOSED and MERGED reach the same GitHub state.
    """

    CLOSED = "CLOSED"
    MERGED = "MERGED"
    REOPENED = "REOPENED"


class ActorRole(StrEnum):
    """How a GitHub user relates to a tracked item.

    Only what GitHub can tell us; a Discord permission tier is `CommandRole` in `discord_bot`.
    Values come and go without a migration: `role_type` is a plain varchar with no constraint.
    """

    AUTHOR = "AUTHOR"
    ASSIGNEE = "ASSIGNEE"
    REVIEWER = "REVIEWER"
    # A team asked for a review, kept apart from the people asked: a person's request is closed
    # when they submit a review, and a team's when any member does, which no payload identifies.
    REVIEWER_TEAM = "REVIEWER_TEAM"


class DeliveryStatus(StrEnum):
    """How far a webhook delivery has got.

    Whether a delivery is still going is asked by the lease, the prune and the partial index
    serving them, so it is answered here once. FAILED means the attempts ran out; the row stays.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"

    @classmethod
    def live(cls) -> tuple[DeliveryStatus, ...]:
        """Still going. Ordered, because the index predicate is built from it and compared."""
        return (cls.PENDING, cls.PROCESSING)

    @classmethod
    def terminal(cls) -> tuple[DeliveryStatus, ...]:
        return tuple(status for status in cls if status not in cls.live())
