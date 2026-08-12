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

            cleared = await ItemAssignmentStore(session).clear_role_for(
                item.id, ActorRole.REVIEWER, snapshot.author.login
            )

        if cleared:
            logger.info(
                "%s reviewed %s#%s, so their review request is closed",
                snapshot.author.login,
                snapshot.repository.full_name,
                snapshot.item_number,
            )
