"""Turning the publishing of a thread on and off.

Issue #103. The two things worth reading closely here are the notice and the ordering.

The notice is posted into the thread before anything is armed, and a thread that refuses it means
the command fails and nothing is captured. Everybody else in the thread is about to have their
words published into a repository, and an ephemeral reply is seen by exactly one person.

The ordering is commit first, then the set. The other way round looks safer, since clearing a set
cannot fail, but a commit that then failed would leave the row open and the set clear, and the next
restart would reload that row and quietly resume publishing people's words after they had been told
it had stopped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import LoggedConversation, Repository, TrackedItem
from shannon.db.stores.conversations import ConversationStore
from shannon.domain.enums import ObjectType
from shannon.services.sync.items import ItemSyncService
from shannon.services.transcripts.log import (
    AlreadyLoggingError,
    CannotLogError,
    ConversationLog,
    NotLoggingError,
)
from shannon.services.workflow import NotAnItemThreadError
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

WHO = 4242
LATER = 4243
FULL_NAME = f"{payloads.OWNER}/{payloads.REPO}"
ITEM = (FULL_NAME, 7)


@pytest.fixture
def clock() -> list[datetime]:
    return [datetime(2026, 9, 18, 14, 0, tzinfo=UTC)]


@pytest.fixture
def log(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    clock: list[datetime],
) -> ConversationLog:
    return ConversationLog(db_sessionmaker, threads, now=lambda: clock[0])


@pytest.fixture
async def thread_id(registered: Repository, sync_service: ItemSyncService, pr_event) -> int:
    result = await sync_service.sync(pr_event("opened"))
    assert result.thread_id is not None
    return result.thread_id


def notices(threads: FakeThreadGateway, thread_id: int) -> list[str]:
    return [said for where, said in threads.posts if where == thread_id]


class TestTurningItOn:
    async def test_it_answers_with_the_item_it_publishes_to(
        self, log: ConversationLog, thread_id: int
    ) -> None:
        assert await log.start(thread_id=thread_id, by=WHO) == ITEM

    async def test_the_thread_is_armed(self, log: ConversationLog, thread_id: int) -> None:
        assert log.is_logging(thread_id) is False

        await log.start(thread_id=thread_id, by=WHO)

        assert log.is_logging(thread_id) is True

    async def test_a_row_records_who_turned_it_on_and_when(
        self, log: ConversationLog, thread_id: int, db_session: AsyncSession, clock: list[datetime]
    ) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        row = await db_session.scalar(select(LoggedConversation))
        assert row is not None
        assert (row.started_by_discord_user_id, row.discord_thread_id) == (WHO, thread_id)
        assert row.started_at == clock[0]
        assert row.stopped_at is None

    async def test_the_thread_is_told_before_anybody_is_recorded(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        said = notices(threads, thread_id)[-1]
        assert "Logging to GitHub is on" in said
        assert f"{FULL_NAME}#7" in said

    async def test_the_notice_says_what_is_not_published(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """The only place anybody learns the rules, so it has to carry them."""
        await log.start(thread_id=thread_id, by=WHO)

        said = notices(threads, thread_id)[-1]
        assert "Bot messages and attachments are not included" in said
        assert "deleting one before it goes out keeps it out" in said

    async def test_a_thread_that_refuses_the_notice_captures_nothing(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """The one ordering here that cannot be turned round. Arming first and announcing after
        can publish the words of people who were never told."""
        threads.post_error = RuntimeError("Discord said no")

        with pytest.raises(RuntimeError):
            await log.start(thread_id=thread_id, by=WHO)

        assert log.is_logging(thread_id) is False


class TestTurningItOff:
    async def test_the_thread_is_disarmed(self, log: ConversationLog, thread_id: int) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        await log.stop(thread_id=thread_id, by=LATER)

        assert log.is_logging(thread_id) is False

    async def test_the_row_is_closed_rather_than_deleted(
        self, log: ConversationLog, thread_id: int, db_session: AsyncSession
    ) -> None:
        """Publishing what people said is worth being able to say afterwards who turned it on."""
        await log.start(thread_id=thread_id, by=WHO)

        await log.stop(thread_id=thread_id, by=LATER)

        row = await db_session.scalar(select(LoggedConversation))
        assert row is not None
        assert row.stopped_at is not None
        assert (row.started_by_discord_user_id, row.stopped_by_discord_user_id) == (WHO, LATER)

    async def test_the_thread_is_told(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        await log.stop(thread_id=thread_id, by=LATER)

        assert "Logging to GitHub is off" in notices(threads, thread_id)[-1]

    async def test_a_thread_that_refuses_the_notice_has_still_stopped(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """The state change has already landed, so reporting a failure would have somebody run it
        again and be told it was never logging."""
        await log.start(thread_id=thread_id, by=WHO)
        threads.post_error = RuntimeError("Discord said no")

        assert await log.stop(thread_id=thread_id, by=LATER) == ITEM
        assert log.is_logging(thread_id) is False

    async def test_it_can_be_logged_again_afterwards(
        self, log: ConversationLog, thread_id: int
    ) -> None:
        """What the partial unique index is for. A plain one would mean once ever."""
        await log.start(thread_id=thread_id, by=WHO)
        await log.stop(thread_id=thread_id, by=WHO)

        await log.start(thread_id=thread_id, by=WHO)

        assert log.is_logging(thread_id) is True


class TestWhatItRefuses:
    async def test_starting_twice(self, log: ConversationLog, thread_id: int) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        with pytest.raises(AlreadyLoggingError, match="already being logged"):
            await log.start(thread_id=thread_id, by=WHO)

    async def test_stopping_what_was_never_started(
        self, log: ConversationLog, thread_id: int
    ) -> None:
        with pytest.raises(NotLoggingError, match="not being logged"):
            await log.stop(thread_id=thread_id, by=WHO)

    async def test_a_thread_this_bot_does_not_track(self, log: ConversationLog) -> None:
        with pytest.raises(NotAnItemThreadError):
            await log.start(thread_id=999999, by=WHO)

    async def test_a_project_board_card(
        self, log: ConversationLog, db_session: AsyncSession, registered: Repository
    ) -> None:
        """A draft card has no comment section to publish anything into."""
        db_session.add(
            TrackedItem(
                repository_id=registered.id,
                github_object_id=555,
                github_object_type=ObjectType.TICKET,
                github_object_number=1,
                github_url="",
                title="A card",
                discord_thread_id=7777,
            )
        )
        await db_session.commit()

        with pytest.raises(CannotLogError, match="board card"):
            await log.start(thread_id=7777, by=WHO)

    async def test_two_people_starting_at_the_same_moment(
        self, log: ConversationLog, thread_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The read above settles the ordinary case; the partial unique index settles this one.

        A pre-check cannot be the authority, because two commands running together both find
        nothing. The insert is what decides, and the loser is told the same thing the reader
        would have told them.
        """

        async def taken(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(ConversationStore, "start", taken)

        with pytest.raises(AlreadyLoggingError, match="already being logged"):
            await log.start(thread_id=thread_id, by=WHO)

        assert log.is_logging(thread_id) is False

    async def test_a_refused_start_tells_nobody(
        self, log: ConversationLog, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """The refusal is checked before the notice, so running it twice does not put a second
        "logging is on" line in front of everybody."""
        await log.start(thread_id=thread_id, by=WHO)
        before = len(notices(threads, thread_id))

        with pytest.raises(AlreadyLoggingError):
            await log.start(thread_id=thread_id, by=WHO)

        assert len(notices(threads, thread_id)) == before


class TestComingBackAfterARestart:
    async def test_what_was_logging_is_armed_again(
        self,
        log: ConversationLog,
        thread_id: int,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Without this every conversation stays armed in the database and captured by nothing,
        and the only sign is a transcript that stopped at the last deploy."""
        await log.start(thread_id=thread_id, by=WHO)

        fresh = ConversationLog(db_sessionmaker, threads)
        await fresh.reload()

        assert fresh.is_logging(thread_id) is True

    async def test_what_was_stopped_is_not(
        self,
        log: ConversationLog,
        thread_id: int,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        await log.start(thread_id=thread_id, by=WHO)
        await log.stop(thread_id=thread_id, by=WHO)

        fresh = ConversationLog(db_sessionmaker, threads)
        await fresh.reload()

        assert fresh.is_logging(thread_id) is False

    async def test_a_process_with_nothing_logging_says_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
    ) -> None:
        fresh = ConversationLog(db_sessionmaker, threads)

        await fresh.reload()

        assert fresh.is_logging(1) is False


class TestWhenTheThreadGoes:
    async def test_a_deleted_thread_stops_being_logged(
        self, log: ConversationLog, thread_id: int, db_session: AsyncSession
    ) -> None:
        await log.start(thread_id=thread_id, by=WHO)

        await log.forget_threads([thread_id])

        assert log.is_logging(thread_id) is False
        row = await db_session.scalar(select(LoggedConversation))
        assert row is not None and row.stopped_at is not None

    async def test_a_whole_channel_going_stops_the_conversations_in_it(
        self, log: ConversationLog, thread_id: int, db_session: AsyncSession
    ) -> None:
        """By item rather than by thread, because that is what deleting a channel answers with:
        the store that clears the pointers reports the items, not the threads."""
        await log.start(thread_id=thread_id, by=WHO)
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None

        await log.forget_items([item.id])

        assert log.is_logging(thread_id) is False

    async def test_a_channel_holding_nothing_of_ours_is_silent(self, log: ConversationLog) -> None:
        await log.forget_items([])
        await log.forget_threads([])


async def test_a_clock_of_its_own_is_used_for_the_stamps(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    thread_id: int,
    db_session: AsyncSession,
) -> None:
    """Injected for the reason it is everywhere else here: a test that has to sleep to prove a
    timestamp is a test nobody runs."""
    at = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(hours=3)
    log = ConversationLog(db_sessionmaker, threads, now=lambda: at)

    await log.start(thread_id=thread_id, by=WHO)

    row = await db_session.scalar(select(LoggedConversation))
    assert row is not None and row.started_at == at
