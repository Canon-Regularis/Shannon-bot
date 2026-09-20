"""Deciding when a captured conversation is worth publishing, and publishing it.

A long-lived task beside the delivery worker, not a user of the delivery queue: that queue is the
one table an operator reads to see what GitHub actually sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.conversations import ConversationStore, PendingBatch
from shannon.db.stores.logged_messages import LoggedMessageStore, mentioned_in
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.errors import ShannonError
from shannon.services.transcripts.lines import Tagged, TranscriptLine
from shannon.services.transcripts.publish import TranscriptPublisher
from shannon.services.workflow import NotAnItemThreadError, locate

logger = logging.getLogger(__name__)

# How long a conversation may run before it is published whatever else is true. Without it a
# thread trickling a message inside each quiet gap never goes quiet and waits for the line count.
LONGEST_A_LINE_WAITS = timedelta(minutes=10)

# How many lines one comment carries: a ceiling, not a target.
MOST_LINES = 40

# And the same in characters, well under GitHub's own 65536 limit on a comment body; `fit_body`
# is the backstop beneath it.
BODY_BUDGET = 40000

# How long a claim is believed before it is taken to belong to a process that died holding it.
# Also the retry cadence after a failure, because a failed flush keeps its claim.
FLUSH_RETRY_AFTER = timedelta(minutes=5)

# How many failures in a row before a batch is given up on. At the cadence above that is most of
# an hour of GitHub refusing the same body, which is past an outage and into the batch itself.
MOST_ATTEMPTS = 6

GAVE_UP = (
    "**This conversation could not be published to GitHub.** {reason}\n"
    "The {count} messages waiting to go out have been dropped. Logging is still on."
)


def should_flush(
    *,
    count: int,
    characters: int,
    oldest: datetime,
    newest: datetime,
    stopped: bool,
    now: datetime,
    quiet_gap: timedelta,
) -> bool:
    """Whether what is waiting is worth a comment yet."""
    if count == 0:
        return False
    if stopped:
        return True
    if count >= MOST_LINES:
        return True
    if characters >= BODY_BUDGET:
        return True
    if now - oldest >= LONGEST_A_LINE_WAITS:
        return True
    return now - newest >= quiet_gap


class TranscriptFlusher:
    """Publishes what has been captured, once it is worth publishing."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: TranscriptPublisher,
        threads: PostsToThread,
        *,
        quiet_gap: timedelta,
        tick: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._publisher = publisher
        self._threads = threads
        self._quiet_gap = quiet_gap
        self._tick = tick
        self._now = now
        self._stopping = False
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()

    async def run_forever(self) -> None:
        """Publish what is ready until asked to stop.

        A failure is logged and waited out rather than ending the loop: a flusher that dies takes
        transcripts with it until a restart, and nothing else would say so.
        """
        while not self._stopping:
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not publish transcripts, carrying on")
            await self._wait()

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=self._tick.total_seconds())
        except TimeoutError:
            return

    async def flush_once(self) -> None:
        """One pass over every conversation with something waiting."""
        async with self._sessionmaker() as session, session.begin():
            # A claim whose batch was deleted in Discord leaves a conversation `pending`
            # cannot see, because it has no rows to join against. Cleared first, so the next
            # thing said in that thread does not wait out the retry window.
            await ConversationStore(session).release_empty_claims()
        async with self._sessionmaker() as session:
            waiting = await ConversationStore(session).pending()
        for batch in waiting:
            try:
                await self._one(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "could not publish the transcript for conversation %s, carrying on",
                    batch.conversation_id,
                )

    async def _one(self, batch: PendingBatch) -> None:
        now = self._now()
        if batch.flush_id is not None:
            through = await self._take_over(batch, now)
        else:
            through = await self._take(batch, now)
        if through is None:
            return
        await self._publish(batch, through)

    async def _take_over(self, batch: PendingBatch, now: datetime) -> int | None:
        """Pick up a claim whose holder is gone; the batch finished is the one it claimed.

        Anything said since waits for the next pass and its own decision.
        """
        held, started, through = batch.flush_id, batch.flush_started_at, batch.flush_through_id
        if held is None or started is None or through is None:
            return None
        if now - started < FLUSH_RETRY_AFTER:
            return None
        async with self._sessionmaker() as session, session.begin():
            seized = await ConversationStore(session).seize(
                batch.conversation_id, flush_id=uuid4().hex, held=held, now=now
            )
        if not seized:
            return None
        logger.info(
            "conversation %s had a flush nobody finished, taking it on", batch.conversation_id
        )
        return through

    async def _take(self, batch: PendingBatch, now: datetime) -> int | None:
        ready = should_flush(
            count=batch.count,
            characters=batch.characters,
            oldest=batch.oldest,
            newest=batch.newest,
            stopped=batch.stopped,
            now=now,
            quiet_gap=self._quiet_gap,
        )
        if not ready:
            return None
        async with self._sessionmaker() as session, session.begin():
            claimed = await ConversationStore(session).claim(
                batch.conversation_id, flush_id=uuid4().hex, through_id=batch.through_id, now=now
            )
        return batch.through_id if claimed else None

    async def _publish(self, batch: PendingBatch, through: int) -> None:
        try:
            found = await locate(self._sessionmaker, batch.discord_thread_id)
        except NotAnItemThreadError:
            # The item or its thread pointer has gone since the messages were captured, so
            # nothing will ever make this batch sendable. Dropped rather than held.
            logger.info(
                "conversation %s has no item any more, dropping what it was holding",
                batch.conversation_id,
            )
            await self._done(batch, through)
            return

        lines = await self._lines(found.guild_id, batch.conversation_id, through)
        if not lines:
            # Everything claimed was deleted in Discord before it went out: what the delete
            # handler exists to produce.
            await self._done(batch, through)
            return

        try:
            await self._publisher.publish(found, lines)
        except ShannonError as error:
            await self._failed(batch, through, error)
            return
        await self._done(batch, through)

    async def _lines(
        self, guild_id: int, conversation_id: int, through: int
    ) -> Sequence[TranscriptLine]:
        """The claimed messages, with the GitHub account `/link` knows each named person by.

        One query for every login the batch needs: a lookup per line would run to hundreds for a
        forty-line batch of authors and everyone they tagged. An author renders as a link and
        somebody tagged as a mention, because being recorded as having spoken is not a request to
        be notified.
        """
        async with self._sessionmaker() as session:
            held = await LoggedMessageStore(session).through(conversation_id, through)
            tagged = [mentioned_in(message) for message in held]
            wanted = {message.discord_author_id for message in held}
            wanted.update(person for named in tagged for person in named)
            logins = await UserLinkStore(session).logins_for(
                guild_id=guild_id, discord_user_ids=wanted
            )
            return [
                TranscriptLine(
                    author_display_name=message.author_display_name,
                    said_at=message.said_at,
                    content=message.content,
                    login=logins.get(message.discord_author_id),
                    tagged={
                        person: Tagged(display_name=name, login=logins.get(person))
                        for person, name in named.items()
                    },
                )
                for message, named in zip(held, tagged, strict=True)
            ]

    async def _done(self, batch: PendingBatch, through: int) -> None:
        """Drop the batch and let the claim go, in one transaction.

        A process that dies between the comment landing on GitHub and this running leaves the rows
        and the claim, so the batch publishes twice once the claim reads as abandoned. Deliberate:
        a duplicate comment can be deleted, a silently missing chunk cannot be noticed.
        """
        async with self._sessionmaker() as session, session.begin():
            await LoggedMessageStore(session).delete_through(batch.conversation_id, through)
            await ConversationStore(session).release(batch.conversation_id)

    async def _failed(self, batch: PendingBatch, through: int, error: ShannonError) -> None:
        """Count the failure, and give up on the batch once there have been enough of them.

        The claim is kept, so the retry waits out `FLUSH_RETRY_AFTER` rather than the next tick.
        """
        async with self._sessionmaker() as session, session.begin():
            failures = await ConversationStore(session).note_failure(batch.conversation_id)
        logger.warning(
            "could not publish the transcript for conversation %s (%s of %s): %s",
            batch.conversation_id,
            failures,
            MOST_ATTEMPTS,
            error.message,
        )
        if failures < MOST_ATTEMPTS:
            return

        # The people whose words were captured are the only ones who will see that they were
        # not published.
        await self._done(batch, through)
        with_reason = GAVE_UP.format(reason=error.message, count=batch.count)
        try:
            await self._threads.post(
                thread_id=batch.discord_thread_id, panel=Panel.of_text(with_reason)
            )
        except Exception:
            logger.warning(
                "could not say in thread %s that a transcript was dropped",
                batch.discord_thread_id,
            )
