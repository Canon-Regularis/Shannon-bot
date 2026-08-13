from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import ThreadNotFoundError, ThreadStartedEmptyError
from shannon.discord_bot.threads import ThreadGateway, ThreadHandle
from shannon.domain.errors import ItemNotReadyError

logger = logging.getLogger(__name__)

# How long a shutdown will wait for a thread that is mid-creation to be attached to its item.
# Long enough for a Discord call that has already been made to come back and a single row to be
# written; short enough that a gateway which has stopped answering cannot hold up the process.
CLAIM_GRACE_SECONDS = 10.0


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

        Creating and claiming are shielded together. The worker puts a deadline on each delivery,
        and a deadline that expired between these two would leave a thread in Discord that no row
        mentions, which the retry has no way to find and so opens another beside.

        Running it as its own task is what keeps it out of reach of the caller's cancellation.
        That much a shield also did, and a shield was what used to be here, but keeping the work
        alive was only half of it: the caller's await raises at once either way, and everything
        above walked away. The worker task ended, shutdown read that as the worker having
        stopped, and the engine was disposed and the loop closed with the claim unfinished, so
        the thread reached Discord and no row ever mentioned it. The missing half is waiting,
        which is what the cancellation path below does before passing the cancellation on.

        Waiting on the task rather than shielding it, because the two protect equally and this
        one leaves a failure readable. A shielded future whose caller has been cancelled has its
        exception reported by asyncio itself, in its own words, at the moment the log is the only
        thing anybody has; done this way the failure is ours to report, and it is reported below.

        Bounded, because shutdown cannot hang on a gateway that has stopped answering. Running
        out is the old behaviour and no worse than it.
        """
        claiming = asyncio.ensure_future(self._create_and_claim(target, name=name, content=content))
        try:
            await asyncio.wait({claiming})
            return claiming.result()
        except asyncio.CancelledError:
            done, _ = await asyncio.wait({claiming}, timeout=CLAIM_GRACE_SECONDS)
            if not done:
                logger.error(
                    "gave up waiting for a thread to be attached to tracked item %s; if one was "
                    "opened it is in the channel with nothing pointing at it",
                    target.tracked_item_id,
                )
            elif not claiming.cancelled() and claiming.exception() is not None:
                logger.warning(
                    "a thread being opened for tracked item %s failed as it was shutting down: %s",
                    target.tracked_item_id,
                    claiming.exception(),
                )
            raise

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
            await self._attach_or_take_back(target, error.thread_id, None)
            raise

        return await self._attach_or_take_back(target, handle.thread_id, handle.message_id)

    async def _attach_or_take_back(
        self, target: ThreadTarget, thread_id: int, message_id: int | None
    ) -> ThreadHandle:
        """Claim the thread, and remove it again if the claim could not be written down.

        A claim that never committed leaves a thread in Discord that no row mentions, and
        nothing anywhere reconciles those: `discord_thread_id` is only ever read off a row found
        some other way, so an id that never reached a row cannot be reached by anything. The
        retry finds no thread, opens a second one, and the first stays in the channel taking no
        comment, review or ping for the rest of its life.

        Taking it back is both possible and right, because of which half failed. Discord answered
        or there would be no thread; it is the database that did not, so the call that undoes it
        is the one still working. Nothing is claimed at the point this can fire either: the swap
        either commits and returns, or matches nothing and commits nothing.

        Best effort, and quiet about it. The delivery is going to be retried on the original
        error, which is the one worth reporting.
        """
        try:
            return await self._claim(target, thread_id, message_id)
        except ItemNotReadyError:
            # The claim ran and decided there was nothing to attach to. It has already tidied up.
            raise
        except Exception:
            logger.warning(
                "could not attach thread %s to tracked item %s, taking it back so the retry "
                "does not open a second one beside it",
                thread_id,
                target.tracked_item_id,
            )
            with contextlib.suppress(Exception):
                await self._threads.delete(thread_id=thread_id)
            raise

    async def _claim(
        self, target: ThreadTarget, thread_id: int, message_id: int | None
    ) -> ThreadHandle:
        """Attach a thread just opened, or stand down if another caller got there first.

        The swap is from the id the item held when this sync started, so a rebuild cannot
        overwrite a replacement somebody else already attached. Only one thread may end up on
        an item; the one that lost is removed rather than left in the channel collecting
        nothing for ever.
        """
        claimed_thread, claimed_message = await self._swap(
            target.tracked_item_id, thread_id, message_id, replacing=target.thread_id
        )

        # Nobody owns the item now. Somebody let go of the old thread while this one was being
        # opened, which is what the note mirror does when a comment finds the thread deleted.
        # The swap missed because it was written from an id that is no longer there, not
        # because another thread won, so this one takes the empty slot rather than being thrown
        # away with the item left holding nothing.
        if claimed_thread is None:
            claimed_thread, claimed_message = await self._swap(
                target.tracked_item_id, thread_id, message_id, replacing=None
            )

        if claimed_thread is None:
            # The item itself has gone: the repository was unregistered, or the row was removed
            # while this was in flight. There is nothing to attach the thread to, so it is
            # tidied away and the delivery is left to be retried rather than reported as done.
            await self._threads.delete(thread_id=thread_id)
            raise ItemNotReadyError(
                f"tracked item {target.tracked_item_id} is no longer there to attach a thread to"
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

    async def _swap(
        self, tracked_item_id: int, thread_id: int, message_id: int | None, *, replacing: int | None
    ) -> tuple[int | None, int | None]:
        async with self._sessionmaker() as session, session.begin():
            return await TrackedItemStore(session).claim_thread(
                tracked_item_id,
                thread_id=thread_id,
                message_id=message_id,
                replacing=replacing,
            )

    async def _remember(self, tracked_item_id: int, handle: ThreadHandle) -> None:
        """Record where the metadata message now lives, which moves if it was deleted.

        Conditional on the item still pointing at the thread that was just written to, for the
        same reason every other write here is: the Discord call happened outside any
        transaction, so the item may have moved on to a different thread in the meantime and
        writing this id back would send it to one somebody has already abandoned.
        """
        await self._swap(
            tracked_item_id,
            handle.thread_id,
            handle.message_id,
            replacing=handle.thread_id,
        )
