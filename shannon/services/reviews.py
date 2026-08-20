from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.models import ReviewSnapshot

logger = logging.getLogger(__name__)


class ReviewRequestLedger:
    """Closes a review request once the review it asked for has been submitted.

    GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
    `pull_request` event saying so. The reviewer ping is driven by the assignment row existing,
    so without following that here the row survives with its `notified_at` set, and clicking
    re-request review reads as "already asked" and tells nobody. That is the one moment the
    feature exists for.

    The request is stamped rather than removed. A delivery captured before the review and
    retried after it still lists the reviewer, and with the row gone it put the request back and
    pinged them to review what they had already approved. The stamp is what a later payload gets
    compared against, so a genuine re-request still reopens it and a straggler does not.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def fulfilled(self, snapshot: ReviewSnapshot) -> None:
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

            assignments = ItemAssignmentStore(session)
            cleared = await assignments.mark_fulfilled(
                item.id, ActorRole.REVIEWER, snapshot.author.login, snapshot.created_at
            )
            # Every team asked, not the reviewer's own. GitHub dismisses a team's request when a
            # member of it reviews and says so in no payload, and nothing here can tell which
            # teams somebody belongs to without asking, which would be a call per review.
            #
            # So this closes them all, and is deliberately wrong in one direction: a team whose
            # member did not review is closed early and goes untold about a re-request. The other
            # direction pings a team about work already reviewed, and this module already picked
            # a side on that, in the same words, for a person.
            await assignments.mark_all_fulfilled(
                item.id, ActorRole.REVIEWER_TEAM, snapshot.created_at
            )

        if cleared:
            logger.info(
                "%s reviewed %s#%s, so their review request is closed",
                snapshot.author.login,
                snapshot.repository.full_name,
                snapshot.item_number,
            )
