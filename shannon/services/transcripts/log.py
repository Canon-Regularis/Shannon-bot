"""Which threads are being captured, and holding what is said in them until it is published.

Issue #103. Two things live here that look unrelated and are not: the set of armed threads, and
the write that fills it. The set is what makes `on_message` affordable, and the rows are what make
it survive a restart, so the two have to move together and in one order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.conversations import ConversationStore
from shannon.db.stores.logged_messages import LoggedMessageStore
from shannon.discord_bot.capture import CapturedMessage
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import ObjectType
from shannon.domain.errors import ShannonError
from shannon.services.workflow import locate

logger = logging.getLogger(__name__)


class AlreadyLoggingError(ShannonError):
    """Somebody asked to start logging a thread that is already being logged."""


class NotLoggingError(ShannonError):
    """Somebody asked to stop logging a thread that was not being logged."""


class CannotLogError(ShannonError):
    """This thread has nowhere on GitHub to publish a conversation to."""


# What the thread is told when logging starts. Visible rather than ephemeral, and that is the
# whole point of it: the ephemeral reply is seen by exactly one person, and that is the person who
# already knows. Everybody else in the thread is about to have their words published into a
# repository, and this is the only place they are told.
STARTED = (
    "**Logging to GitHub is on.** Everything said in this thread from now on is published as a "
    "comment on {full_name}#{number}, with your name on it.\n"
    "Bot messages and attachments are not included, editing a message afterwards does not change "
    "what is published, and deleting one before it goes out keeps it out.\n"
    "Run `/stop_conversation` to stop."
)

STOPPED = "**Logging to GitHub is off.** Nothing said in this thread is published from now on."


class ConversationLog:
    """The threads being captured, and the messages waiting to go out of them."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._now = now
        self._live: set[int] = set()
        self._said_nothing_arrived = False

    async def reload(self) -> None:
        """Fill the set from the rows, for a process that has just started.

        Called before the gateway connects, so no message can arrive against a half-filled set.
        Without it every conversation would be armed in the database and captured by nothing, and
        the only sign would be a transcript that stopped at the last deploy.
        """
        async with self._sessionmaker() as session:
            threads = await ConversationStore(session).live_threads()
        self._live = set(threads)
        if self._live:
            logger.info("%s conversations are still being logged", len(self._live))

    def is_logging(self, thread_id: int) -> bool:
        """Whether this channel is being captured. Synchronous, and that is deliberate.

        `on_message` fires for every message in every channel of every server this bot is in. This
        is what it asks first, so a message in a thread nobody armed costs one set lookup. An
        async answer would allocate a coroutine and a task step for every message in the server,
        and a database answer would be a scan of `tracked_items`, whose thread column carries no
        index on purpose.
        """
        return thread_id in self._live

    async def start(self, *, thread_id: int, by: int) -> tuple[str, int]:
        """Begin logging this thread, answering with the item it publishes to.

        The notice goes into the thread BEFORE anything is armed, and a refusal from Discord ends
        the command. That ordering is the one thing here that cannot be turned round: arming first
        and announcing afterwards can capture people who were never told, and a process that died
        in between would leave a conversation that reloads armed with no notice ever posted.

        The cost of this way round is a notice posted for nothing when two people run the command
        at the same moment and the insert below refuses the second. That is the cheaper mistake.
        """
        found = await locate(self._sessionmaker, thread_id)
        if found.object_type is ObjectType.TICKET:
            raise CannotLogError(
                "A project board card has no GitHub comments to publish a conversation to."
            )

        async with self._sessionmaker() as session:
            if await ConversationStore(session).open_for_thread(thread_id) is not None:
                raise AlreadyLoggingError("This thread is already being logged to GitHub.")

        await self._threads.post(
            thread_id=thread_id,
            content=STARTED.format(full_name=found.full_name, number=found.number),
        )

        async with self._sessionmaker() as session, session.begin():
            opened = await ConversationStore(session).start(
                tracked_item_id=found.tracked_item_id,
                discord_thread_id=thread_id,
                started_by=by,
                now=self._now(),
            )
        if opened is None:
            raise AlreadyLoggingError("This thread is already being logged to GitHub.")

        # After the commit, never before. Clearing or setting the set first would leave the two
        # disagreeing if the write then failed, and the next restart reads the rows.
        self._live.add(thread_id)
        logger.info("logging %s#%s from thread %s", found.full_name, found.number, thread_id)
        return found.full_name, found.number

    async def stop(self, *, thread_id: int, by: int) -> tuple[str, int]:
        """End logging, leaving whatever is pending to be published.

        What has been captured and not yet sent is deliberately not thrown away. The flusher reads
        a stopped conversation as a reason to publish at once, so this is what gets the tail of the
        conversation onto GitHub rather than what loses it.
        """
        found = await locate(self._sessionmaker, thread_id)

        async with self._sessionmaker() as session, session.begin():
            stopped = await ConversationStore(session).stop(
                tracked_item_id=found.tracked_item_id, stopped_by=by, now=self._now()
            )
        if stopped is None:
            raise NotLoggingError("This thread is not being logged to GitHub.")

        self._live.discard(thread_id)
        logger.info("stopped logging %s#%s", found.full_name, found.number)

        # The state change has already landed, so a thread that will not take the notice is not a
        # reason to tell whoever ran the command that it failed. They would try again and be told
        # it was never logging.
        try:
            await self._threads.post(thread_id=thread_id, content=STOPPED)
        except Exception:
            logger.warning("could not say in thread %s that logging stopped", thread_id)
        return found.full_name, found.number

    async def capture(self, message: CapturedMessage) -> None:
        """Keep one message until the flusher wants it.

        A read before the write, because the set can be ahead of the rows: a conversation stopped
        while this message was in flight leaves the thread armed for as long as it takes the set to
        catch up. Finding no open conversation is the answer, not an error.
        """
        async with self._sessionmaker() as session, session.begin():
            store = ConversationStore(session)
            conversation_id = await store.open_for_thread(message.thread_id)
            if conversation_id is None:
                self._live.discard(message.thread_id)
                return
            await LoggedMessageStore(session).add(
                conversation_id=conversation_id,
                discord_message_id=message.message_id,
                discord_author_id=message.author_id,
                author_display_name=message.author_display_name,
                content=message.content,
                said_at=message.said_at,
            )

    def nothing_to_capture(self, thread_id: int) -> None:
        """Note a message in a logged thread that arrived with no content at all.

        Once per process, because the case worth catching is the message content intent granted in
        name only, which makes every message empty and is indistinguishable from a thread where
        people only post pictures. Without this that takes an hour to work out.
        """
        if self._said_nothing_arrived:
            return
        self._said_nothing_arrived = True
        logger.info(
            "a message in logged thread %s carried no text. If that is true of every message, "
            "the message content intent is not really granted, and nothing will be published.",
            thread_id,
        )

    async def forget(self, message_ids: Sequence[int]) -> None:
        """Drop messages deleted in Discord before they were published."""
        async with self._sessionmaker() as session, session.begin():
            await LoggedMessageStore(session).forget(message_ids)

    async def forget_threads(self, thread_ids: Sequence[int]) -> None:
        """Stop logging threads that have gone, so nothing stays armed against a dead one."""
        async with self._sessionmaker() as session, session.begin():
            await ConversationStore(session).stop_for_threads(thread_ids, now=self._now())
        self._live.difference_update(thread_ids)

    async def forget_items(self, tracked_item_ids: Sequence[int]) -> None:
        """The same, for a whole channel's worth of threads going at once.

        By item because that is what deleting a channel answers with: the store that clears the
        thread pointers reports the items it cleared, not the threads they were pointing at.
        """
        async with self._sessionmaker() as session, session.begin():
            stopped = await ConversationStore(session).stop_for_items(
                tracked_item_ids, now=self._now()
            )
        self._live.difference_update(stopped)
