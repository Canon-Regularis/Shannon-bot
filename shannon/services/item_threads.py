from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import ThreadNotFoundError, ThreadStartedEmptyError
from shannon.discord_bot.threads import ThreadGateway, ThreadHandle

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ThreadTarget:
    """Which item is being written to, and where its thread is if it has one yet."""

    tracked_item_id: int
    channel_id: int
    thread_id: int | None
    message_id: int | None


@dataclass(frozen=True, slots=True)
class ThreadWrite:
    handle: ThreadHandle
    created: bool


class ItemThreads:
    """Keeps one tracked item pointing at exactly one Discord thread.

    Opening a thread is a network call, so it cannot happen inside the transaction that decided
    one was needed. Everything awkward about that gap lives here rather than being spread
    through the sync flow: two callers both finding no thread and both opening one, a thread
    deleted out from under an item, and a thread that opens but cannot be written to.
    """

    def __init__(self, sessionmaker: async_sessionmaker, threads: ThreadGateway) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads

    async def write(self, target: ThreadTarget, *, name: str, content: str) -> ThreadWrite:
        """Put `content` in the item's thread, opening or rebuilding one where needed."""
        if target.thread_id is None:
            return ThreadWrite(await self._open(target, name=name, content=content), created=True)

        try:
            handle = await self._threads.update(
                thread_id=target.thread_id,
                message_id=target.message_id,
                name=name,
                content=content,
            )
        except ThreadNotFoundError:
            # Somebody deleted the thread. Its id is worthless now, and holding on to it would
            # fail this event and every later one for the item, so the item gets a fresh thread
            # rather than going quiet for the rest of its life.
            logger.info(
                "thread %s for tracked item %s is gone, opening a replacement",
                target.thread_id,
                target.tracked_item_id,
            )
            return ThreadWrite(await self._open(target, name=name, content=content), created=True)

        await self._remember(target.tracked_item_id, handle)
        return ThreadWrite(handle, created=False)

    async def _open(self, target: ThreadTarget, *, name: str, content: str) -> ThreadHandle:
        """Open a thread and attach it, replacing whatever the item pointed at before.

        Creating and claiming are shielded together. The worker puts a deadline on each
        delivery, and a deadline that expired between these two would leave a thread in Discord
        that no row mentions, which the retry has no way to find and so opens another beside.
        """
        return await asyncio.shield(self._create_and_claim(target, name=name, content=content))

    async def _create_and_claim(
        self, target: ThreadTarget, *, name: str, content: str
    ) -> ThreadHandle:
        try:
            handle = await self._threads.create(
                channel_id=target.channel_id, name=name, content=content
            )
        except ThreadStartedEmptyError as error:
            # The thread is real even though its first message never landed. Recording it here
            # means the retry writes into it instead of opening another one beside it.
            await self._claim(target, error.thread_id, None)
            raise

        return await self._claim(target, handle.thread_id, handle.message_id)

    async def _claim(
        self, target: ThreadTarget, thread_id: int, message_id: int | None
    ) -> ThreadHandle:
        """Attach a thread just opened, or stand down if another caller got there first.

        The swap is from the id the item held when this sync started, so a rebuild cannot
        overwrite a replacement somebody else already attached. Only one thread may end up on
        an item; the one that lost is removed rather than left in the channel collecting
        nothing for ever.
        """
        async with self._sessionmaker() as session, session.begin():
            claimed_thread, claimed_message = await TrackedItemStore(session).claim_thread(
                target.tracked_item_id,
                thread_id=thread_id,
                message_id=message_id,
                replacing=target.thread_id,
            )

        if claimed_thread == thread_id:
            return ThreadHandle(thread_id=thread_id, message_id=claimed_message)

        logger.warning(
            "another sync attached thread %s to tracked item %s first, discarding %s",
            claimed_thread,
            target.tracked_item_id,
            thread_id,
        )
        await self._threads.delete(thread_id=thread_id)
        return ThreadHandle(thread_id=claimed_thread, message_id=claimed_message)

    async def _remember(self, tracked_item_id: int, handle: ThreadHandle) -> None:
        """Record where the metadata message now lives, which moves if it was deleted."""
        async with self._sessionmaker() as session, session.begin():
            items = TrackedItemStore(session)
            item = await items.get_by_id(tracked_item_id)
            if item is not None:
                await items.set_discord_ids(
                    item, thread_id=handle.thread_id, message_id=handle.message_id
                )
