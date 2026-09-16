"""Putting a thread back the way the row says it should be, after something wrote to it.

Discord refuses every edit to an archived thread, so every write reopens one first. That is what
makes writing to a finished item work at all, and it is also what undoes the shut: the closing
header is posted a moment after the sync shuts the thread, and a comment on an issue somebody
closed last week arrives whenever it arrives. Both leave the thread open behind them.

So the shut is not a thing done once when an item finishes. It is a thing restored after anything
writes, and this is what restores it.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.threads import ShutsThread

logger = logging.getLogger(__name__)


class KeepsThreadsShut:
    """Shut a thread again, if the row says that is what it should be."""

    def __init__(self, sessionmaker: async_sessionmaker, threads: ShutsThread) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads

    async def again(self, *, tracked_item_id: int, thread_id: int) -> None:
        """Called after a post has landed, never before one.

        The row is asked rather than the caller, and that is the whole design. What has to be
        restored is not what the thread was a moment ago but what the item says it should be:
        a thread Discord archived by itself after a week of quiet on an item still open wants
        leaving open, and one this bot shut wants shutting again. Only the row tells them apart.
        It is also the same fact the closing header reads for its wording, so the two cannot end
        up contradicting each other.

        A refusal is swallowed. The post has already landed and its claim is spent, so raising
        would hand back a claim for a line that was said. What it leaves behind is a thread
        locked but not archived, which is exactly what this project shipped for a year, and
        Discord's own archive window closes it within the week anyway.
        """
        if not await self._should_be_shut(tracked_item_id):
            return

        try:
            await self._threads.set_shut(thread_id=thread_id, shut=True)
        except DiscordGatewayError as refusal:
            # One arm for all of them. A thread deleted between the post and here, a permission
            # taken away, and Discord having a bad minute all mean the same thing to this: the
            # line was said, the thread is not shut, and the next delivery for this item will
            # find the row still asking and try again.
            logger.warning(
                "could not shut the thread for tracked item %s again after writing to it: %s",
                tracked_item_id,
                refusal,
            )

    async def _should_be_shut(self, tracked_item_id: int) -> bool:
        async with self._sessionmaker() as session:
            found = await TrackedItemStore(session).get_with_its_server(tracked_item_id)
        return found is not None and found[0].discord_thread_locked is True
