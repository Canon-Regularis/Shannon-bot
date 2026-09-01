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

            # Only the reviewer's own row. A team's request is closed by GitHub dropping it from
            # `requested_teams`, which deletes the row on the next delivery, and that is the whole
            # mechanism a team needs.
            #
            # An earlier version stamped every team row here on the reasoning that a team can be
            # answered by any member and no payload says which. It was wrong twice over. The
            # double ping it meant to prevent cannot happen, because `replace` leaves an existing
            # row alone and a row that has been pinged keeps its `notified_at`. And the stamp it
            # wrote made the row look like an answered request, so `reopen_if_newer` cleared both
            # stamps on the next ordinary event with a later timestamp and pinged the role again,
            # once per review round, for a request nobody had answered or re-made.
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
