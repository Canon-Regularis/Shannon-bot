"""Which threads are being captured, and holding what is said in them until it is published.

The set of armed threads is what makes `on_message` affordable, and the rows are what make it
survive a restart, so the two have to move together and in one order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.conversations import ConversationStore
from shannon.db.stores.logged_messages import LoggedMessageStore
from shannon.discord_bot.capture import CapturedMessage
from shannon.discord_bot.panels import Panel
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


# What the thread is told when logging starts. Visible rather than ephemeral: an ephemeral reply
# reaches only the person who already knows, and everybody else in the thread is about to have
# their words published into a repository.
STARTED = (
    "**Logging to GitHub is on.** Everything said in this thread from now on is published as a "
    "comment on {full_name}#{number}, with your name on it.\n"
    "Bot messages and attachments are not included, editing a message afterwards does not change "
    "what is published, and deleting one before it goes out keeps it out.\n"
    "Run /stop_conversation to stop."
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
        """
        async with self._sessionmaker() as session:
            threads = await ConversationStore(session).live_threads()
        self._live = set(threads)
        if self._live:
            logger.info("%s conversations are still being logged", len(self._live))

    def is_logging(self, thread_id: int) -> bool:
        """Whether this channel is being captured, kept synchronous on purpose.

        `on_message` asks this for every message in every channel. An async answer costs a
        coroutine each time; a database answer would scan the unindexed `tracked_items` thread.
        """
        return thread_id in self._live

    async def start(self, *, thread_id: int, by: int) -> tuple[str, int]:
        """Begin logging this thread, answering with the item it publishes to.

        The notice goes in before anything is armed, since arming first can capture people who
        were never told. A race costs a notice posted for nothing when the insert below refuses.
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
            panel=Panel.of_text(STARTED.format(full_name=found.full_name, number=found.number)),
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

        # After the commit, never before: setting it first leaves the set disagreeing with the
        # rows if the write fails, and the next restart reads the rows.
        self._live.add(thread_id)
        logger.info("logging %s#%s from thread %s", found.full_name, found.number, thread_id)
        return found.full_name, found.number

    async def stop(self, *, thread_id: int, by: int) -> tuple[str, int]:
        """End logging, leaving whatever is pending to be published.

        The flusher reads a stopped conversation as a reason to publish at once, so the tail of
        the conversation still reaches GitHub.
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
        # failure to report: they would try again and be told it was never logging.
        try:
            await self._threads.post(thread_id=thread_id, panel=Panel.of_text(STOPPED))
        except Exception:
            logger.warning("could not say in thread %s that logging stopped", thread_id)
        return found.full_name, found.number

    async def capture(self, message: CapturedMessage) -> None:
        """Keep one message until the flusher wants it.

        The set can be ahead of the rows: a conversation stopped while this message was in flight
        leaves the thread armed until the set catches up, so no open conversation is not an error.
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
                mentions=message.mentions,
            )

    def nothing_to_capture(self, thread_id: int) -> None:
        """Note a message in a logged thread that arrived with no content at all.

        Once per process. The case worth catching is the message content intent granted in name
        only, which empties every message and looks like a thread of nothing but pictures.
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

        By item because the store that clears the thread pointers reports the items it cleared,
        not the threads they were pointing at.
        """
        async with self._sessionmaker() as session, session.begin():
            stopped = await ConversationStore(session).stop_for_items(
                tracked_item_ids, now=self._now()
            )
        self._live.difference_update(stopped)
