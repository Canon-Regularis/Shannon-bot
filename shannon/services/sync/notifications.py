from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import PostsToThread
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
        threads: PostsToThread,
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
        # Deliberately not shielded. Shielding this looks like it protects the claim, and does
        # the opposite: the await raises at once while the claim carries on and commits, so
        # `logins` is never bound, the guard below is never entered, and the ping is owed to
        # nobody for ever. Unshielded, a cancellation here aborts the transaction before it
        # commits and nothing was claimed, which is the outcome worth having. Once this returns
        # there is no await before the guard, so nothing can land in between.
        logins, mentions = await self._claim(tracked_item_id, guild_id)
        if not logins:
            return ()

        try:
            await self._threads.post(thread_id=thread_id, content=self._render(logins, mentions))
        except BaseException:
            # Nothing was said, so the ping is owed again; late beats twice or never.
            #
            # BaseException because cancellation counts as a failure here: the worker puts a
            # deadline on each delivery and cancels the handler where it stands, and discord.py
            # sleeps through a rate limit rather than failing, so where it stands is often
            # exactly here. Shielded so that cancellation cannot interrupt the hand-back itself.
            # Under cancellation the await returns at once and the release lands a moment later,
            # well before the retry five seconds on.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._release(tracked_item_id, logins))
            raise

        await self._record_mentions(tracked_item_id, mentions)
        logger.info("pinged %s %s on tracked item %s", self._role, logins, tracked_item_id)
        return logins

    async def _claim(
        self, tracked_item_id: int, guild_id: int
    ) -> tuple[tuple[str, ...], Mapping[str, int]]:
        """Take the pings nobody has sent yet, and work out how to address them."""
        async with self._sessionmaker() as session, session.begin():
            logins = tuple(
                sorted(
                    await ItemAssignmentStore(session).claim_notifications(
                        tracked_item_id, self._role
                    )
                )
            )
            if not logins:
                return (), {}
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=guild_id, github_usernames=logins
            )
        return logins, mentions

    async def _release(self, tracked_item_id: int, logins: tuple[str, ...]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).release_notifications(
                tracked_item_id, self._role, logins
            )

    async def _record_mentions(self, tracked_item_id: int, mentions: Mapping[str, int]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).record_discord_ids(tracked_item_id, mentions)
