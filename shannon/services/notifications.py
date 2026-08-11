from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.formatting import format_reviewer_ping
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole

logger = logging.getLogger(__name__)


class ReviewerNotifier:
    """Pings reviewers once each.

    `item_assignments.notified_at` is the record of who has already been told. A reviewer who
    is removed and requested again gets a fresh row, and so gets pinged again, which is what
    someone re-requesting a review is asking for.
    """

    def __init__(self, sessionmaker: async_sessionmaker, threads: ThreadGateway) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int
    ) -> tuple[str, ...]:
        """Ping reviewers who have not been pinged yet, returning the logins that were."""
        async with self._sessionmaker() as session:
            pending = await ItemAssignmentStore(session).pending_notification(
                tracked_item_id, ActorRole.REVIEWER
            )
            logins = tuple(sorted(row.github_username for row in pending))
            if not logins:
                return ()
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=guild_id, github_usernames=logins
            )

        await self._threads.post(
            thread_id=thread_id, content=format_reviewer_ping(logins, mentions)
        )

        # Stamped only once the message is out, so a Discord failure leaves the ping owed
        # rather than silently swallowed.
        await self._mark_notified(tracked_item_id, logins, mentions)
        logger.info("pinged reviewers %s on tracked item %s", logins, tracked_item_id)
        return logins

    async def _mark_notified(
        self, tracked_item_id: int, logins: tuple[str, ...], mentions: dict[str, int]
    ) -> None:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            assignments = ItemAssignmentStore(session)
            for row in await assignments.pending_notification(tracked_item_id, ActorRole.REVIEWER):
                if row.github_username not in logins:
                    continue
                row.notified_at = now
                discord_user_id = mentions.get(row.github_username)
                if discord_user_id is not None:
                    row.discord_user_id = discord_user_id
