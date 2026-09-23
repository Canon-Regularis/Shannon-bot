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


# What a state is called in front of a person. Issue #147.
#
# A table rather than a transformation of `.value`, because the enum's own spelling is three other
# things at once: the GitHub label name (`github/labels.py`), the varchar in `tracked_items`, and
# the key a priority label is case-folded against. SCREAMING_SNAKE is what all three want and none
# of them is a sentence, so the display form has to be its own answer and never the same string.
#
# Written out for the reason `services/workflow._OWNED_BY` is written out: a derived form would be
# right for eight of these and silently wrong for the ninth, and there is nowhere in a
# `.replace().capitalize()` to put UNSET's word.
_SPOKEN: dict[Status | Priority, str] = {
    Status.NOT_REVIEWED: "Not reviewed",
    Status.IN_REVIEW: "In review",
    Status.READY_FOR_MERGE: "Ready for merge",
    Status.BACKLOG: "Backlog",
    Status.DONE: "Done",
    Priority.HIGH: "High",
    Priority.MEDIUM: "Medium",
    Priority.LOW: "Low",
    # The absence of a priority rather than one of them, which is why no GitHub label answers to
    # it. The word is the one the card already uses for a field with nothing in it.
    Priority.UNSET: "None",
}


def spoken(state: Status | Priority) -> str:
    """What to call a status or a priority in front of a person.

    Total on purpose: a missing key is an arm nothing could reach and a test holds the table
    against both enums instead.
    """
    return _SPOKEN[state]


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


class VerificationPurpose(StrEnum):
    """Which command asked for a one-time link, and so what spending it finishes.

    The callback is a browser arriving unauthenticated, so the row is the only record of what the
    person was in the middle of. `/link` is finished by the click itself; `/unregister` is
    deliberately not, because the permission check and the unbinding both need somebody to report
    the answer to.

    Values come and go without a migration: the column is a plain varchar with no constraint,
    which is what `varchar_enum` buys.
    """

    LINK = "LINK"
    UNREGISTER = "UNREGISTER"
