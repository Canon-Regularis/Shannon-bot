from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole

logger = logging.getLogger(__name__)

Renderer = Callable[[Sequence[str], Mapping[str, int]], str]


class ActorNotifier:
    """Pings the people in one role once each.

    `item_assignments.notified_at` is the record of who has already been told. Someone removed
    and then added back gets a fresh row, and so is pinged again, which is what re-requesting a
    review or reassigning an issue is asking for.

    The role and the wording are injected, because a reviewer being asked to review and an
    assignee being handed a ticket are the same mechanism with different words.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: ThreadGateway,
        *,
        role: ActorRole,
        render: Renderer,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._role = role
        self._render = render

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int
    ) -> tuple[str, ...]:
        """Ping whoever has not been pinged yet, returning the logins that were.

        The pings are claimed before the message goes out, not after. Two syncs of one item
        overlap whenever somebody runs /pr while an event for it is in flight, and a delivery
        that fails partway through is retried from the top; either would send the same person
        the same ping twice if the claim came last.
        """
        async with self._sessionmaker() as session, session.begin():
            logins = tuple(
                sorted(
                    await ItemAssignmentStore(session).claim_notifications(
                        tracked_item_id, self._role
                    )
                )
            )
            if not logins:
                return ()
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=guild_id, github_usernames=logins
            )

        try:
            await self._threads.post(thread_id=thread_id, content=self._render(logins, mentions))
        except Exception:
            # Nothing was said, so the ping is owed again. Being pinged late is a great deal
            # better than being pinged twice or not at all.
            await self._release(tracked_item_id, logins)
            raise

        await self._record_mentions(tracked_item_id, mentions)
        logger.info("pinged %s %s on tracked item %s", self._role, logins, tracked_item_id)
        return logins

    async def _release(self, tracked_item_id: int, logins: tuple[str, ...]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).release_notifications(
                tracked_item_id, self._role, logins
            )

    async def _record_mentions(self, tracked_item_id: int, mentions: Mapping[str, int]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).record_discord_ids(tracked_item_id, mentions)
