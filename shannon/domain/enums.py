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


class ActorRole(StrEnum):
    """How a GitHub user relates to a tracked item.

    Only what GitHub can tell us. `PROJECT_MANAGER` was here and is gone: it is a Discord
    permission tier, and there is no fact about a pull request or an issue that produces one, so
    nothing ever wrote it and nothing could have. The tier still exists where it belongs, as
    `CommandRole` in `discord_bot/roles.py`.

    Neither the removal nor the addition below needs a migration. `role_type` is a plain varchar
    with no constraint, which `varchar_enum` explains, and no row can hold a value nothing ever
    wrote.
    """

    AUTHOR = "AUTHOR"
    ASSIGNEE = "ASSIGNEE"
    REVIEWER = "REVIEWER"
    # A team asked for a review, kept apart from the people asked. Apart because the two are
    # told in different words and closed by different rules: a person's request is answered when
    # they submit a review, and a team's is answered when any of its members does, which no
    # payload identifies. Sharing one role would leave a team row that nothing could ever close.
    REVIEWER_TEAM = "REVIEWER_TEAM"


class DeliveryStatus(StrEnum):
    """How far a webhook delivery has got.

    A delivery is either still going or finished with, and which is which is asked in three
    places: the lease, the prune, and the partial index that serves them. Answering it here
    means adding a sixth state cannot leave one of the three behind. FAILED means the attempts
    ran out, and the row is kept with its reason so someone can see what happened.
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
