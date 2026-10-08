"""A reminder going off, from the table to the channel. Issue #229.

Against the real database with a clock this file holds, so "five minutes later" is a line rather
than a wait. What is pinned: it goes off where it was asked for and rings only the person it is
for; it says when it is late; an item's thread it went off in is put back the way its row says;
what Discord answers decides whether it is dropped, kept for later or given up on; it goes out once
however many senders ask; and the loop around it waits for Discord, stops when asked, and survives
a pass that fails.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Reminder, Repository
from shannon.db.stores.reminders import ReminderStore
from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
)
from shannon.services.reminders.send import MOST_ATTEMPTS, RETRY_AFTER, ReminderSender
from shannon.services.sync.items import ItemSyncService
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway
from tests.support.waiting import until

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CHANNEL = 30
MEMBER = 20
AUTHOR = 10
CLOSED = {"state": "closed", "closed_at": "2026-08-11T12:00:00Z"}


@pytest.fixture
def clock() -> list[datetime]:
    """What the sender believes the time is. A test moves it by assigning `clock[0]`."""
    return [AT]


def sender_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    clock: list[datetime],
    *,
    tick: timedelta = timedelta(minutes=30),
) -> ReminderSender:
    return ReminderSender(
        sessionmaker,
        threads,
        KeepsThreadsShut(sessionmaker, threads),
        tick=tick,
        now=lambda: clock[0],
    )


@pytest.fixture
def sender(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    clock: list[datetime],
) -> ReminderSender:
    return sender_for(db_sessionmaker, threads, clock)


async def plant(
    session: AsyncSession,
    *,
    due_at: datetime = AT,
    channel: int = CHANNEL,
    set_by: int = AUTHOR,
    message: str | None = "look at the river",
) -> int:
    """One reminder written down, as `/remind` leaves it."""
    reminder_id = await ReminderStore(session).add(
        guild_id=1,
        channel_id=channel,
        member_id=MEMBER,
        set_by=set_by,
        message=message,
        set_at=due_at - timedelta(hours=3),
        due_at=due_at,
    )
    await session.commit()
    return reminder_id


async def left(session: AsyncSession) -> list[Reminder]:
    """What is still owed, read from the table rather than from the session's own copies."""
    session.expire_all()
    return list((await session.scalars(select(Reminder).order_by(Reminder.id))).all())


async def connected() -> None:
    """A gateway that is already there."""


class TestGoingOff:
    async def test_nothing_goes_off_before_it_is_due(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session, due_at=AT + timedelta(minutes=1))

        assert await sender.send_due() == 0
        assert threads.channel_posts == []

    async def test_it_goes_off_where_it_was_asked_for_and_rings_only_its_person(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Whoever asked for it is named and not rung; the person it is for is both."""
        await plant(db_session)

        assert await sender.send_due() == 1

        kind, where, content, notify = threads.allowed[-1]
        assert (kind, where, notify) == ("post_in_channel", CHANNEL, (MEMBER,))
        assert content.startswith("### ⏰ Reminder")
        assert f"<@{MEMBER}> — <@{AUTHOR}> asked for this" in content
        assert "look at the river" in content
        assert await left(db_session) == []

    async def test_one_that_goes_off_on_time_says_nothing_about_time(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session)

        await sender.send_due()

        assert "could not be sent until now" not in threads.channel_posts[-1][1]

    async def test_one_that_fell_due_while_nothing_was_running_says_so(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """A restart delivers what fell due while it was down, and a reader is owed knowing it was
        not meant for now."""
        await plant(db_session, due_at=AT - timedelta(hours=2))

        await sender.send_due()

        assert "could not be sent until now" in threads.channel_posts[-1][1]

    async def test_everything_due_goes_in_one_pass_earliest_first(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session, due_at=AT - timedelta(minutes=1), message="second")
        await plant(db_session, due_at=AT - timedelta(minutes=5), message="first")

        assert await sender.send_due() == 2

        said = [content for _, content in threads.channel_posts]
        assert ["first" in said[0], "second" in said[1]] == [True, True]


class TestInAnItemsThread:
    async def test_a_shut_items_thread_is_left_shut(
        self,
        sender: ReminderSender,
        registered: Repository,
        issue_service: ItemSyncService,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """Asked for in the thread while the item was open; gone off after it closed."""
        result = await issue_service.sync(issue_event("opened"))
        await issue_service.sync(issue_event("closed", **CLOSED))
        thread_id = result.thread_id
        assert thread_id is not None
        await plant(db_session, channel=thread_id)

        await sender.send_due()

        assert threads.channel_posts[-1][0] == thread_id
        assert threads.threads[thread_id].archived is True
        assert threads.shut_calls[-1] == (thread_id, True)

    async def test_an_open_items_thread_is_left_open(
        self,
        sender: ReminderSender,
        registered: Repository,
        issue_service: ItemSyncService,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        result = await issue_service.sync(issue_event("opened"))
        assert result.thread_id is not None
        asked = len(threads.shut_calls)
        await plant(db_session, channel=result.thread_id)

        await sender.send_due()

        assert threads.shut_calls[asked:] == []


class TestWhenDiscordWillNot:
    @pytest.mark.parametrize(
        "refusal",
        [
            ChannelNotFoundError("the channel has gone"),
            DiscordPermissionError("this bot may not post there any more"),
        ],
    )
    async def test_somewhere_it_will_never_go_off_drops_it(
        self,
        sender: ReminderSender,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        refusal: Exception,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await plant(db_session)
        threads.channel_post_error = refusal

        with caplog.at_level(logging.WARNING, logger="shannon.services.reminders.send"):
            assert await sender.send_due() == 1

        assert await left(db_session) == []
        assert "never will" in caplog.text
        assert "look at the river" not in caplog.text, "somebody's message was written to the log"

    async def test_a_refusal_that_may_pass_keeps_it_for_later(
        self,
        sender: ReminderSender,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        clock: list[datetime],
    ) -> None:
        """Kept, and not asked about again until its claim has lapsed: a bad minute on Discord's
        side is not worth a refusal every five seconds."""
        await plant(db_session)
        threads.channel_post_error = DiscordGatewayError("Discord is having a bad minute")

        await sender.send_due()

        [kept] = await left(db_session)
        assert (kept.failed_attempts, kept.claimed_at) == (1, AT)

        threads.channel_post_error = None
        clock[0] = AT + RETRY_AFTER - timedelta(seconds=1)
        assert await sender.send_due() == 0

        clock[0] = AT + RETRY_AFTER
        assert await sender.send_due() == 1
        assert len(threads.channel_posts) == 1
        assert await left(db_session) == []

    async def test_it_is_given_up_on_after_enough_refusals(
        self,
        sender: ReminderSender,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reminder_id = await plant(db_session)
        await db_session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(failed_attempts=MOST_ATTEMPTS - 1)
        )
        await db_session.commit()
        threads.channel_post_error = DiscordGatewayError("still down")

        with caplog.at_level(logging.ERROR, logger="shannon.services.reminders.send"):
            await sender.send_due()

        assert await left(db_session) == []
        assert f"after {MOST_ATTEMPTS} attempts" in caplog.text

    async def test_one_refusal_short_of_the_limit_is_still_kept(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        reminder_id = await plant(db_session)
        await db_session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(failed_attempts=MOST_ATTEMPTS - 2)
        )
        await db_session.commit()
        threads.channel_post_error = DiscordGatewayError("still down")

        await sender.send_due()

        [kept] = await left(db_session)
        assert kept.failed_attempts == MOST_ATTEMPTS - 1

    async def test_something_that_is_not_discord_answering_is_left_to_the_claim(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """A bug is not a refusal, so it counts no attempt; the claim lapses and it is tried
        again, and the loop around this logs it."""
        await plant(db_session)
        threads.channel_post_error = RuntimeError("a bug")

        with pytest.raises(RuntimeError, match="a bug"):
            await sender.send_due()

        [kept] = await left(db_session)
        assert (kept.failed_attempts, kept.claimed_at) == (0, AT)


class TestOnceOnly:
    async def test_two_senders_asking_together_send_it_once(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        clock: list[datetime],
    ) -> None:
        await plant(db_session)
        one = sender_for(db_sessionmaker, threads, clock)
        two = sender_for(db_sessionmaker, threads, clock)

        settled = await asyncio.gather(one.send_due(), two.send_due())

        assert sorted(settled) == [0, 1]
        assert len(threads.channel_posts) == 1

    async def test_a_stop_is_honoured_before_the_next_reminder(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session)
        sender.stop()

        assert await sender.send_due() == 0
        assert threads.channel_posts == []


class TestTheLoop:
    async def test_nothing_goes_off_before_discord_connects(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session)
        ready = asyncio.Event()

        async def when_ready() -> None:
            await ready.wait()

        running = asyncio.create_task(sender.run_forever(when_ready))
        await asyncio.sleep(0.05)
        assert threads.channel_posts == [], "it posted before Discord was there"

        ready.set()
        await until(lambda: bool(threads.channel_posts))
        sender.stop()
        await running

    async def test_a_stop_before_discord_connects_ends_it(
        self, sender: ReminderSender, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await plant(db_session)

        async def never() -> None:
            await asyncio.Event().wait()

        running = asyncio.create_task(sender.run_forever(never))
        await asyncio.sleep(0.05)
        sender.stop()
        await asyncio.wait_for(running, timeout=5)

        assert threads.channel_posts == []

    async def test_a_bot_that_stopped_before_it_connected_is_raised(
        self, sender: ReminderSender
    ) -> None:
        async def failed() -> None:
            raise RuntimeError("the Discord bot stopped before it ever connected")

        with pytest.raises(RuntimeError, match="before it ever connected"):
            await sender.run_forever(failed)

    async def test_a_stop_wakes_a_long_tick(self, sender: ReminderSender) -> None:
        """Ticking every half hour here, so an ending that waited for the tick would not end."""
        running = asyncio.create_task(sender.run_forever(connected))
        await asyncio.sleep(0.05)

        sender.stop()

        await asyncio.wait_for(running, timeout=5)

    async def test_a_pass_that_fails_does_not_end_the_loop(
        self,
        sender: ReminderSender,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The only test here that lets a tick run out rather than be woken."""
        await plant(db_session)
        monkeypatch.setattr(sender, "_tick", timedelta(milliseconds=10))
        real = sender.send_due
        passes: list[int] = []

        async def one_bad_pass() -> int:
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("a pass that fell over")
            return await real()

        monkeypatch.setattr(sender, "send_due", one_bad_pass)

        with caplog.at_level(logging.ERROR, logger="shannon.services.reminders.send"):
            running = asyncio.create_task(sender.run_forever(connected))
            await until(lambda: bool(threads.channel_posts))
            sender.stop()
            await running

        assert "could not send reminders, carrying on" in caplog.text

    async def test_cancelling_it_ends_it(self, sender: ReminderSender) -> None:
        running = asyncio.create_task(sender.run_forever(connected))
        await asyncio.sleep(0.05)

        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running
