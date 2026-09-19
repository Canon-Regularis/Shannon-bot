"""Holding what is said in a logged thread until it is published.

Issue #103. The rows exist because GitHub can be down: an in-memory buffer facing a failed write
either grows without bound or drops the batch, and dropping it loses part of a conversation with
nothing anywhere saying so.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import LoggedMessage, Repository
from shannon.db.stores.logged_messages import mentioned_in
from shannon.discord_bot.capture import CapturedMessage
from shannon.services.sync.items import ItemSyncService
from shannon.services.transcripts.log import ConversationLog
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

WHO = 4242
AT = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)


@pytest.fixture
def log(
    db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
) -> ConversationLog:
    return ConversationLog(db_sessionmaker, threads)


@pytest.fixture
async def thread_id(registered: Repository, sync_service: ItemSyncService, pr_event) -> int:
    result = await sync_service.sync(pr_event("opened"))
    assert result.thread_id is not None
    return result.thread_id


@pytest.fixture
async def logging_thread(log: ConversationLog, thread_id: int) -> int:
    await log.start(thread_id=thread_id, by=WHO)
    return thread_id


def said(
    thread_id: int,
    *,
    message_id: int = 501,
    content: str = "hello",
    minute: int = 0,
    mentions: Mapping[int, str] | None = None,
) -> CapturedMessage:
    return CapturedMessage(
        thread_id=thread_id,
        message_id=message_id,
        author_id=77,
        author_display_name="alice",
        content=content,
        said_at=AT + timedelta(minutes=minute),
        mentions=mentions or {},
    )


async def held(session: AsyncSession) -> list[LoggedMessage]:
    found = await session.scalars(select(LoggedMessage).order_by(LoggedMessage.id))
    return list(found)


class TestKeepingWhatWasSaid:
    async def test_a_message_is_kept(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        await log.capture(said(logging_thread))

        rows = await held(db_session)
        assert len(rows) == 1
        assert (rows[0].content, rows[0].author_display_name) == ("hello", "alice")
        assert rows[0].said_at == AT

    async def test_several_are_kept_in_the_order_they_were_said(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        await log.capture(said(logging_thread, message_id=501, content="first"))
        await log.capture(said(logging_thread, message_id=502, content="second", minute=1))

        assert [row.content for row in await held(db_session)] == ["first", "second"]

    async def test_the_same_message_twice_is_kept_once(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        """discord.py redelivers events after a resumed session. Doing nothing on a conflict is
        also what makes editing a message a no-op without a rule anywhere saying so."""
        await log.capture(said(logging_thread, content="hello"))
        await log.capture(said(logging_thread, content="hello, edited"))

        rows = await held(db_session)
        assert len(rows) == 1
        assert rows[0].content == "hello", "an edit changed what was already recorded"

    async def test_a_thread_that_is_not_being_logged_keeps_nothing(
        self, log: ConversationLog, thread_id: int, db_session: AsyncSession
    ) -> None:
        """The set can be ahead of the rows: a conversation stopped while a message was in flight
        leaves the thread armed until the set catches up. Finding nothing is the answer."""
        await log.capture(said(thread_id))

        assert await held(db_session) == []

    async def test_and_the_thread_stops_being_armed(
        self, log: ConversationLog, logging_thread: int
    ) -> None:
        await log.stop(thread_id=logging_thread, by=WHO)
        # Put it back in the set by hand, the way a stop that raced a message would leave it.
        log._live.add(logging_thread)

        await log.capture(said(logging_thread))

        assert log.is_logging(logging_thread) is False


class TestTakingOneBack:
    async def test_a_deleted_message_is_dropped_before_it_goes_out(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        """Retraction rather than accuracy, which is why this exists and editing does not. Delete
        it before it is published and it is not published, which is a rule somebody can act on."""
        await log.capture(said(logging_thread, message_id=501))
        await log.capture(said(logging_thread, message_id=502))

        await log.forget([501])

        assert [row.discord_message_id for row in await held(db_session)] == [502]

    async def test_a_purge_drops_all_of_them(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        await log.capture(said(logging_thread, message_id=501))
        await log.capture(said(logging_thread, message_id=502))

        await log.forget([501, 502])

        assert await held(db_session) == []

    async def test_deleting_one_that_was_never_kept_is_silent(
        self, log: ConversationLog, logging_thread: int
    ) -> None:
        await log.forget([999])

    async def test_deleting_nothing_at_all_asks_the_database_nothing(
        self, log: ConversationLog, logging_thread: int
    ) -> None:
        await log.forget([])


async def test_a_message_in_a_thread_with_no_text_is_noted_once(
    log: ConversationLog, logging_thread: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A message content intent granted in name only makes every message empty, which looks
    exactly like a thread where people post nothing but pictures."""
    with caplog.at_level("INFO", logger="shannon.services.transcripts.log"):
        log.nothing_to_capture(logging_thread)
        log.nothing_to_capture(logging_thread)

    assert caplog.text.count("carried no text") == 1, "said on every empty message, not once"


class TestWhoAMessageTagged:
    """Issue #121. The ids have to survive the row, because a display name is not something the
    GitHub side can turn back into an account."""

    async def test_the_map_is_kept_with_the_message(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        await log.capture(
            said(
                logging_thread,
                content="hey <@111111111111111111>",
                mentions={111111111111111111: "Alice"},
            )
        )

        rows = await held(db_session)
        assert rows[0].content == "hey <@111111111111111111>"
        assert mentioned_in(rows[0]) == {111111111111111111: "Alice"}

    async def test_a_message_that_tagged_nobody_keeps_an_empty_one(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        await log.capture(said(logging_thread, content="hello"))

        rows = await held(db_session)
        assert rows[0].mentions == {}
        assert mentioned_in(rows[0]) == {}

    async def test_a_key_that_is_not_an_id_is_stepped_over(
        self, log: ConversationLog, logging_thread: int, db_session: AsyncSession
    ) -> None:
        """Capture is the only writer, so any other shape could only be a hand edit, and a whole
        transcript refusing to publish over one is the worse failure."""
        await log.capture(
            said(logging_thread, content="hi", mentions={111111111111111111: "Alice"})
        )
        rows = await held(db_session)
        rows[0].mentions = {"111111111111111111": "Alice", "not-an-id": "Nobody"}
        await db_session.commit()

        db_session.expire_all()
        assert mentioned_in((await held(db_session))[0]) == {111111111111111111: "Alice"}
