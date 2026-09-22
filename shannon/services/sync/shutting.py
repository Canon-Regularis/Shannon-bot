"""Putting a thread back the way the row says it should be, after something wrote to it.

Discord refuses every edit to an archived thread, so every write reopens one first and leaves it
open behind it. The shut is therefore not done once when an item finishes but restored after
anything writes, including the closing header posted a moment after the sync shuts the thread.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.threads import ShutsThread

logger = logging.getLogger(__name__)


class KeepsThreadsShut:
    """Shut a thread again, if the row says that is what it should be."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], threads: ShutsThread
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads

    async def again(self, *, tracked_item_id: int, thread_id: int) -> None:
        """Called after a post has landed, never before one.

        The row is asked rather than the caller: a thread Discord archived by itself after a week
        of quiet on an item still open wants leaving open, and one this bot shut wants shutting
        again. A refusal is swallowed, because the post has landed and its claim is spent; what
        it leaves behind is a thread locked but not archived, which Discord's own archive window
        closes within the week.
        """
        if not await self._should_be_shut(tracked_item_id):
            return

        try:
            await self._threads.set_shut(thread_id=thread_id, shut=True)
        except DiscordGatewayError as refusal:
            # A thread deleted between the post and here, a permission taken away and Discord
            # having a bad minute all mean the same thing: the next delivery for this item finds
            # the row still asking and tries again.
            logger.warning(
                "could not shut the thread for tracked item %s again after writing to it: %s",
                tracked_item_id,
                refusal,
            )

    async def _should_be_shut(self, tracked_item_id: int) -> bool:
        async with self._sessionmaker() as session:
            found = await TrackedItemStore(session).get_with_its_server(tracked_item_id)
        return found is not None and found[0].discord_thread_locked is True
