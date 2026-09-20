from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.discord_bot.errors import ThreadNotFoundError, ThreadStartedEmptyError
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import Notify, OpensThreads, ThreadHandle
from shannon.domain.errors import ItemNotReadyError

logger = logging.getLogger(__name__)

# How long shutdown waits for a mid-creation thread to be attached: long enough for a Discord
# call already made to come back, short enough that a dead gateway cannot hold the process open.
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
    # remember, which is every thread claimed before that column existed. `/set_channel` moves
    # where new threads go and leaves the old ones where they were, which is issue #78.
    thread_channel_id: int | None = None

    @property
    def is_stranded(self) -> bool:
        """Whether the thread it has is somewhere the mapping no longer names.

        A row that remembers no channel is unknown, not stranded: every thread claimed before
        that column existed would otherwise qualify, on no evidence.
        """
        return self.thread_channel_id is not None and self.thread_channel_id != self.channel_id


@dataclass(frozen=True, slots=True)
class ThreadWrite:
    handle: ThreadHandle
    created: bool
    # The thread this write moved the item off; None unless relocation was asked for.
    displaced: int | None = None


class ItemThreads:
    """Keeps one tracked item pointing at exactly one Discord thread.

    Opening a thread is a network call outside the transaction that decided one was needed.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: OpensThreads,
        *,
        relocates: bool = False,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        # Whether a stranded thread is replaced by one in the channel the mapping now names. Off
        # on the delivery path: relocating is four to seven Discord calls inside work the worker
        # deadlines and retries, and it leaves behind a thread the queue has nobody to tell about.
        self._relocates = relocates

    async def write(
        self,
        target: ThreadTarget,
        *,
        name: str,
        panel: Panel,
        replacement: Panel | None = None,
        notify: Notify = None,
    ) -> ThreadWrite:
        """Put `panel` in the item's thread, opening or rebuilding one where needed.

        A thread opened to replace one gets `replacement`, the same block with the people on it
        named in plain text: posting a block notifies everybody it mentions, and a thread opened
        because the old one was deleted or its channel moved is nothing happening to the item.
        """
        instead = panel if replacement is None else replacement
        if target.thread_id is None:
            return ThreadWrite(
                await self._open(target, name=name, panel=panel, notify=notify), created=True
            )

        if self._relocates and target.is_stranded:
            # Discord cannot move a thread between channels, so the item gets a new one and the
            # old is left behind for the caller to sign-post and shut.
            logger.info(
                "thread %s for tracked item %s is in channel %s and belongs in %s, replacing it",
                target.thread_id,
                target.tracked_item_id,
                target.thread_channel_id,
                target.channel_id,
            )
            handle = await self._open(target, name=name, panel=instead, notify=notify)
            # Unconditional: after a successful swap the id that comes back is never the old one
            # and after a lost race it is the winner's, so the old thread is displaced either way.
            return ThreadWrite(handle, created=True, displaced=target.thread_id)

        try:
            handle = await self._threads.update(
                thread_id=target.thread_id,
                message_id=target.message_id,
                name=name,
                panel=panel,
                notify=notify,
            )
        except ThreadNotFoundError:
            # Somebody deleted the thread. Holding on to its id would fail this event and every
            # later one for the item, so the item gets a fresh thread.
            logger.info(
                "thread %s for tracked item %s is gone, opening a replacement",
                target.thread_id,
                target.tracked_item_id,
            )
            return ThreadWrite(
                await self._open(target, name=name, panel=instead, notify=notify), created=True
            )

        await self._remember(target.tracked_item_id, handle)
        return ThreadWrite(handle, created=False)

    async def _open(
        self, target: ThreadTarget, *, name: str, panel: Panel, notify: Notify = None
    ) -> ThreadHandle:
        """Open a thread and attach it, out of reach of the caller's cancellation.

        A deadline or a shutdown can cancel between Discord creating the thread and the row
        being told about it, and nothing reconciles orphans: the retry opens a second thread.
        """
        claiming = asyncio.ensure_future(
            self._create_and_claim(target, name=name, panel=panel, notify=notify)
        )
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
        self, target: ThreadTarget, *, name: str, panel: Panel, notify: Notify = None
    ) -> ThreadHandle:
        try:
            handle = await self._threads.create(
                channel_id=target.channel_id,
                name=name,
                panel=panel,
                notify=notify,
            )
        except ThreadStartedEmptyError as error:
            # The thread is real even though its first message never landed. Recording it means
            # the retry writes into it instead of opening another one beside it.
            await self._attach_or_take_back(target, error.thread_id, None)
            raise

        return await self._attach_or_take_back(target, handle.thread_id, handle.message_id)

    async def _attach_or_take_back(
        self, target: ThreadTarget, thread_id: int, message_id: int | None
    ) -> ThreadHandle:
        """Claim the thread, and remove it again if the claim could not be written down.

        Discord answered or there would be no thread, so the database is the part that is down
        and the call undoing it still works; the swap commits or matches nothing, so the failed
        claim leaves nothing behind.
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
        overwrite a replacement somebody else already attached. The thread that lost is removed
        rather than left in the channel collecting nothing for ever.
        """
        claimed_thread, claimed_message = await self._swap(
            target.tracked_item_id,
            thread_id,
            message_id,
            replacing=target.thread_id,
            channel_id=target.channel_id,
        )

        # Nobody owns the item now: something let go of the old thread while this one was being
        # opened, which the note mirror does when a comment finds the thread deleted. The swap
        # missed on an id that is no longer there, not to another thread, so this one takes it.
        if claimed_thread is None:
            claimed_thread, claimed_message = await self._swap(
                target.tracked_item_id,
                thread_id,
                message_id,
                replacing=None,
                channel_id=target.channel_id,
            )

        if claimed_thread is None:
            # The item has gone: the repository was unregistered, or the row was removed while
            # this was in flight. The thread is tidied away and the delivery left to be retried.
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

        Conditional on the item still pointing at that thread: the Discord call happened outside
        any transaction, so the item may have moved on and this id would point it at a thread
        somebody has already abandoned.
        """
        await self._swap(
            tracked_item_id,
            handle.thread_id,
            handle.message_id,
            replacing=handle.thread_id,
        )
