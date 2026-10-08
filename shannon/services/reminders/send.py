"""Sending a reminder once its time comes. Issue #229.

A long-lived task beside the delivery worker, in the transcript flusher's shape and not a user of
the delivery queue: that queue is the one table an operator reads to see what GitHub sent, and a
reminder was never sent by GitHub. It waits for Discord, then wakes every tick, takes whatever has
fallen due one at a time, and posts each where it was asked for.

Taking one is a lease (`ReminderStore.claim_next`), so a reminder goes out at least once rather
than at most once. A process that stops between Discord taking the post and the row being deleted
sends it again once the claim is `RETRY_AFTER` old; a duplicate is visible and harmless, and a
lost reminder is neither.

What Discord answers decides what becomes of a reminder:

- Somewhere that has gone, or that this bot may no longer post in, is permanent. The reminder is
  dropped and said in the log, because no wait will bring the channel back.
- Anything else Discord refuses is tried again once the claim lapses, up to `MOST_ATTEMPTS` - the
  two hours the delivery queue also rides out - and then given up on, said in the log as an error.
- Anything that is not Discord answering at all, a bug or the database, is logged by the loop and
  left to the lapsing claim, uncounted, for the flusher's reason: an attempt is a thing Discord
  refused, not a thing this process got wrong.

The message somebody typed is never logged. A log is read by people the channel it was said in
was not.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.reminders import DueReminder, ReminderStore
from shannon.discord_bot.formatting import format_reminder
from shannon.discord_bot.threads import PostsInChannels
from shannon.domain.errors import PermanentError, ShannonError

logger = logging.getLogger(__name__)

# How long a claim is believed before it is taken to belong to a sender that stopped holding it.
# Also the wait before a reminder Discord refused is tried again, because a refused send keeps its
# claim. Longer than discord.py sits out a rate limit inside one post, so a send still in flight is
# never taken over.
RETRY_AFTER = timedelta(minutes=5)

# How many refusals before a reminder is given up on: two hours at `RETRY_AFTER`, the window the
# delivery queue rides out.
MOST_ATTEMPTS = 24

# How late a reminder has to be before it says so. They are set to the minute.
LATE_AFTER = timedelta(minutes=1)

# What the lifespan hands over to wait for Discord, in the worker's shape.
ReadyCheck = Callable[[], Awaitable[None]]


class ShutsWhereItPosted(Protocol):
    """Putting an item's thread back the way its row says, knowing only where a post went."""

    async def after_posting_in(self, *, channel_id: int) -> None: ...


class ReminderSender:
    """Sends each reminder once it falls due, until stopped."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        channels: PostsInChannels,
        shut_again: ShutsWhereItPosted,
        *,
        tick: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._channels = channels
        self._shut_again = shut_again
        self._tick = tick
        self._now = now
        self._stopping = False
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()

    async def run_forever(self, wait_for_ready: ReadyCheck) -> None:
        """Send reminders until stopped, once Discord is there to send them to.

        A failure is logged and waited out rather than ending the loop: a sender that dies takes
        every reminder with it until a restart, and nothing else would say so. Cancellation is not
        an `Exception` and goes straight through, which is how a shutdown that ran out of grace
        ends this.
        """
        if not await self._connected_or_stopped(wait_for_ready):
            return
        while not self._stopping:
            try:
                await self.send_due()
            except Exception:
                logger.exception("could not send reminders, carrying on")
            await self._wait()

    async def send_due(self) -> int:
        """Send everything that has fallen due, answering how many were settled.

        One at a time, each taken in its own transaction, so a stop between two is honoured and
        no row is held locked while Discord is asked anything.
        """
        settled = 0
        while not self._stopping:
            now = self._now()
            async with self._sessionmaker() as session, session.begin():
                due = await ReminderStore(session).claim_next(
                    now=now, stale_before=now - RETRY_AFTER
                )
            if due is None:
                break
            await self._send(due, now)
            settled += 1
        return settled

    async def _send(self, due: DueReminder, now: datetime) -> None:
        panel = format_reminder(
            member_id=due.member_id,
            set_by=due.set_by,
            set_at=due.set_at,
            due_at=due.due_at,
            message=due.message,
            late=now - due.due_at >= LATE_AFTER,
        )
        try:
            # Only the person it is for. Whoever asked for it is named and not rung, and
            # `/mentions off` is not asked: a reminder is a person addressing somebody, not this
            # bot reporting on an item.
            await self._channels.post_in_channel(
                channel_id=due.channel_id, panel=panel, notify=(due.member_id,)
            )
        except PermanentError as refusal:
            await self._finish(due.reminder_id)
            logger.warning(
                "reminder %s cannot go off in channel %s and never will, so it is dropped: %s",
                due.reminder_id,
                due.channel_id,
                refusal,
            )
            return
        except ShannonError as refusal:
            await self._refused(due, refusal)
            return

        # Deleted before the thread is put back, so nothing that goes wrong putting it back can
        # make the reminder go off twice.
        await self._finish(due.reminder_id)
        logger.info(
            "reminder %s went off in channel %s for %s",
            due.reminder_id,
            due.channel_id,
            due.member_id,
        )
        await self._shut_again.after_posting_in(channel_id=due.channel_id)

    async def _refused(self, due: DueReminder, refusal: ShannonError) -> None:
        """Count a refusal, and give up on the reminder once there have been enough."""
        async with self._sessionmaker() as session, session.begin():
            attempts = await ReminderStore(session).note_failure(due.reminder_id)
        if attempts < MOST_ATTEMPTS:
            logger.warning(
                "reminder %s could not go off in channel %s yet, attempt %s of %s: %s",
                due.reminder_id,
                due.channel_id,
                attempts,
                MOST_ATTEMPTS,
                refusal,
            )
            return
        await self._finish(due.reminder_id)
        logger.error(
            "reminder %s could not go off in channel %s after %s attempts, so it is dropped: %s",
            due.reminder_id,
            due.channel_id,
            attempts,
            refusal,
        )

    async def _finish(self, reminder_id: int) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ReminderStore(session).finish(reminder_id)

    async def _connected_or_stopped(self, wait_for_ready: ReadyCheck) -> bool:
        """Wait for Discord, and give up the moment a stop is asked for instead.

        No deadline, unlike the worker's: nothing can be posted while the gateway is down, so going
        ahead without it would only spend every overdue reminder's attempts on refusals. An error
        from the wait is raised, since a bot that stopped before it connected is a failure.
        """
        ready = asyncio.ensure_future(wait_for_ready())
        stopped = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({ready, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            if not ready.done():
                ready.cancel()

        # Checked before the flag, so a gateway that failed is reported rather than read as an
        # ordinary stop when both finish together.
        if ready.done() and not ready.cancelled() and (failed := ready.exception()) is not None:
            raise failed
        return not self._stopping

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=self._tick.total_seconds())
        except TimeoutError:
            return
