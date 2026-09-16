from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.discord_bot.errors import ThreadNotFoundError, ThreadStartedEmptyError
from shannon.discord_bot.threads import OpensThreads, ThreadHandle
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
    # Where a NEW thread belongs, off the mapping as it stands now.
    channel_id: int
    thread_id: int | None
    message_id: int | None
    # Where the thread it already has actually is, off the row. None where the row does not
    # remember, which is every thread claimed before that column existed.
    #
    # These two are different questions and the whole of issue #78 is that nothing asked the
    # second one. `/set_channel` moves where new threads go and leaves the old ones where they
    # were, so an item registered into the wrong channel kept being written to there for ever.
    thread_channel_id: int | None = None

    @property
    def is_stranded(self) -> bool:
        """Whether the thread it has is somewhere the mapping no longer names.

        Unknown is not stranded. A row that remembers no channel is a candidate for somebody who
        can ask Discord, and this is not that: guessing here would abandon a working thread on no
        evidence, and every row written before the column existed would qualify.
        """
        return self.thread_channel_id is not None and self.thread_channel_id != self.channel_id


@dataclass(frozen=True, slots=True)
class ThreadWrite:
    handle: ThreadHandle
    created: bool
    # The thread this write moved the item off, for a caller that has something to say in it.
    # None on every ordinary write, which is all of them unless relocation was asked for.
    displaced: int | None = None


class ItemThreads:
    """Keeps one tracked item pointing at exactly one Discord thread.

    Opening a thread is a network call, so it cannot happen inside the transaction that decided
    one was needed. Everything awkward about that gap lives here rather than being spread
    through the sync flow: two callers both finding no thread and both opening one, a thread
    deleted out from under an item, and a thread that opens but cannot be written to.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: OpensThreads,
        *,
        relocates: bool = False,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        # Whether a thread in a channel the mapping no longer names is replaced by one in the
        # channel it does. Built with it rather than told per call, the same way the refresh path
        # is built without a notifier: a binding that cannot relocate cannot be talked into it by
        # a later edit, and the delivery path must not.
        #
        # Not on the delivery path because a relocation is four to seven Discord calls, one of
        # them a thread creation, inside something the worker deadlines, cancels and retries
        # sixteen times. It also leaves a thread behind that somebody has to be told about, and
        # the queue has nobody to tell.
        self._relocates = relocates

    async def write(
        self, target: ThreadTarget, *, name: str, content: str, replacement: str | None = None
    ) -> ThreadWrite:
        """Put `content` in the item's thread, opening or rebuilding one where needed.

        A thread opened to REPLACE one gets `replacement`, which is the same block with the
        people on it named in plain text. Opening a thread POSTS its block and a posted message
        notifies everybody it mentions; the two branches below open one because somebody deleted
        the old thread or moved the channel out from under it. Neither is anything happening to
        the item, and neither is worth telling everybody on it about.
        """
        instead = content if replacement is None else replacement
        if target.thread_id is None:
            return ThreadWrite(await self._open(target, name=name, content=content), created=True)

        if self._relocates and target.is_stranded:
            # Discord cannot move a thread between channels, so the item gets a new one and the
            # old is left behind for the caller to sign-post and shut. The swap below is one
            # guarded UPDATE from the old id to the new, so nothing here can leave the row
            # pointing at nothing: it either moves whole or does not move.
            logger.info(
                "thread %s for tracked item %s is in channel %s and belongs in %s, replacing it",
                target.thread_id,
                target.tracked_item_id,
                target.thread_channel_id,
                target.channel_id,
            )
            handle = await self._open(target, name=name, content=instead)
            # Unconditional, and it has to be. After a successful swap the id that comes back is
            # never the old one, and after a lost race it is the winner's, so the old thread is
            # displaced either way and is worth saying so about either way.
            return ThreadWrite(handle, created=True, displaced=target.thread_id)

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
            return ThreadWrite(await self._open(target, name=name, content=instead), created=True)

        await self._remember(target.tracked_item_id, handle)
        return ThreadWrite(handle, created=False)

    async def _open(self, target: ThreadTarget, *, name: str, content: str) -> ThreadHandle:
        """Open a thread and attach it, out of reach of the caller's cancellation.

        The worker deadlines every delivery and shutdown cancels outright, and either can land
        between Discord creating the thread and the row being told about it. Nothing reconciles
        orphans, so the retry opens a second thread beside the first.

        Running it as its own task keeps it alive. Waiting for it on the way out is the other
        half, since the caller's await raises immediately. A shield does the same job but lets
        asyncio report the failure in its own words, and the shutdown log is all anybody gets.
        The wait is bounded so a gateway that has stopped answering cannot hold the process open.
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

        Taking it back is safe because of which half failed. Discord answered or there would be
        no thread, so the database is the part that is down and the call undoing the thread still
        works. Nothing is claimed at this point either: the swap commits and returns, or matches
        nothing and commits nothing.

        Best effort. The delivery is retried on the original error, which is the one worth
        reporting.
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
            target.tracked_item_id,
            thread_id,
            message_id,
            replacing=target.thread_id,
            channel_id=target.channel_id,
        )

        # Nobody owns the item now. Somebody let go of the old thread while this one was being
        # opened, which is what the note mirror does when a comment finds the thread deleted.
        # The swap missed because it was written from an id that is no longer there, not
        # because another thread won, so this one takes the empty slot rather than being thrown
        # away with the item left holding nothing.
        if claimed_thread is None:
            claimed_thread, claimed_message = await self._swap(
                target.tracked_item_id,
                thread_id,
                message_id,
                replacing=None,
                channel_id=target.channel_id,
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
        self,
        tracked_item_id: int,
        thread_id: int,
        message_id: int | None,
        *,
        replacing: int | None,
        channel_id: int | None = None,
    ) -> tuple[int | None, int | None]:
        async with self._sessionmaker() as session, session.begin():
            return await ThreadPointerStore(session).claim_thread(
                tracked_item_id,
                thread_id=thread_id,
                message_id=message_id,
                replacing=replacing,
                channel_id=channel_id,
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
