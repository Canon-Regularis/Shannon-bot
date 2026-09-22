from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.models import ItemNote, ReviewSnapshot

logger = logging.getLogger(__name__)


class ReviewRequestLedger:
    """Closes a review request once the review it asked for has been submitted.

    GitHub drops a reviewer from `requested_reviewers` the moment they submit and sends no
    `pull_request` event saying so; the ping is driven by the assignment row existing, so without
    this the row survives with its `notified_at` set and re-request review reads as "already
    asked". Stamped rather than removed, because a delivery captured before the review and
    retried after it still lists the reviewer: a later payload is compared against the stamp, so
    a genuine re-request reopens it and a straggler does not.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def fulfilled(self, snapshot: ItemNote) -> None:
        if snapshot.author is None:
            return

        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                return

            item = await TrackedItemStore(session).get_by_number(
                repository_id=repository.id,
                number=snapshot.item_number,
                object_type=ObjectType.PR,
            )
            if item is None:
                return

            # Only the reviewer's own row. A team's request is closed by GitHub dropping it
            # from `requested_teams`, which deletes the row on the next delivery; stamping a team
            # row here makes it look answered, and `reopen_if_newer` then pings the role again on
            # the next event with a later timestamp, once per review round.
            cleared = await ItemAssignmentStore(session).mark_fulfilled(
                item.id,
                ActorRole.REVIEWER,
                snapshot.author.login,
                snapshot.created_at,
                snapshot.author.github_user_id,
            )

        if cleared:
            logger.info(
                "%s reviewed %s#%s, so their review request is closed",
                snapshot.author.login,
                snapshot.repository.full_name,
                snapshot.item_number,
            )


def is_worth_a_message(snapshot: ItemNote) -> bool:
    """Whether a submitted review says anything its inline comments do not.

    GitHub wraps every inline note in a review, so leaving notes or replying to somebody else's
    note submits a `commented` review with no body of its own, and mirroring that posts
    `**alice** left a review` with nothing underneath. For the other two verdicts the verdict is
    the content, so an approval with no body still posts.

    Asked by the mirror rather than by the parser: a review this declines still has to run the
    ledger that closes the request it answers, or the reviewer is pinged again for the review
    they just gave.
    """
    assert isinstance(snapshot, ReviewSnapshot)
    return snapshot.verdict != "commented" or bool(snapshot.body.strip())
