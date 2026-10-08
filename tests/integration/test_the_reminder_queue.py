"""The reminder queue, against a real database. Issue #229.

What a reminder needs from its table is three promises. It is taken earliest first; a sender's
claim keeps every other sender off it until the claim goes stale; and two senders asking at the
same moment take different rows rather than the same one. The last is `SKIP LOCKED`, which only
Postgres can show, so these run against it rather than against a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Reminder
from shannon.db.stores.reminders import DueReminder, ReminderStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
# Claims stamped before this belong to a sender that stopped; the sender's own window is five
# minutes, and nothing here depends on the number.
STALE = NOW - timedelta(minutes=5)


async def remember(
    session: AsyncSession,
    *,
    due_in: timedelta,
    guild: int = 1,
    set_by: int = 10,
    member: int = 20,
    message: str | None = "look at #77 before the release",
) -> int:
    """One reminder written and committed, due `due_in` from `NOW` (negative is overdue)."""
    reminder_id = await ReminderStore(session).add(
        guild_id=guild,
        channel_id=30,
        member_id=member,
        set_by=set_by,
        message=message,
        set_at=NOW - timedelta(days=1),
        due_at=NOW + due_in,
    )
    await session.commit()
    return reminder_id


async def claimed_at(session: AsyncSession, reminder_id: int) -> datetime | None:
    """Read from the table rather than the identity map, which an UPDATE ... RETURNING bypasses."""
    return await session.scalar(select(Reminder.claimed_at).where(Reminder.id == reminder_id))


class TestWritingOneDown:
    async def test_it_is_kept_as_it_was_given(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=timedelta(hours=1))

        row = await db_session.get(Reminder, reminder_id)

        assert row is not None
        assert (row.discord_guild_id, row.discord_channel_id) == (1, 30)
        assert (row.discord_user_id, row.set_by_discord_user_id) == (20, 10)
        assert row.message == "look at #77 before the release"
        assert row.due_at == NOW + timedelta(hours=1)
        assert (row.claimed_at, row.failed_attempts) == (None, 0)

    async def test_one_with_nothing_to_say_keeps_no_message(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=timedelta(hours=1), message=None)

        row = await db_session.get(Reminder, reminder_id)

        assert row is not None
        assert row.message is None


class TestCountingWhatIsWaiting:
    """The cap's question: how much one person has asked this bot to say later, in one server."""

    async def test_every_reminder_a_person_set_here_counts(self, db_session: AsyncSession) -> None:
        """Whoever they are for, themselves included."""
        await remember(db_session, due_in=timedelta(hours=1))
        await remember(db_session, due_in=timedelta(hours=2), member=10)

        assert await ReminderStore(db_session).waiting_from(guild_id=1, set_by=10) == 2

    async def test_somebody_elses_and_another_servers_do_not(
        self, db_session: AsyncSession
    ) -> None:
        await remember(db_session, due_in=timedelta(hours=1), set_by=11)
        await remember(db_session, due_in=timedelta(hours=1), guild=2)

        assert await ReminderStore(db_session).waiting_from(guild_id=1, set_by=10) == 0


class TestTakingOne:
    async def test_nothing_due_is_nothing_taken(self, db_session: AsyncSession) -> None:
        await remember(db_session, due_in=timedelta(minutes=1))

        assert await ReminderStore(db_session).claim_next(now=NOW, stale_before=STALE) is None

    async def test_one_due_this_very_moment_is_due(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=timedelta(0))

        taken = await ReminderStore(db_session).claim_next(now=NOW, stale_before=STALE)

        assert taken is not None
        assert taken.reminder_id == reminder_id

    async def test_the_one_that_fell_due_first_goes_first(self, db_session: AsyncSession) -> None:
        """Written second and due first, so the order cannot be the order they were written."""
        await remember(db_session, due_in=-timedelta(minutes=1))
        earlier = await remember(db_session, due_in=-timedelta(minutes=5))

        taken = await ReminderStore(db_session).claim_next(now=NOW, stale_before=STALE)

        assert taken is not None
        assert taken.reminder_id == earlier

    async def test_it_carries_everything_the_message_needs(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=-timedelta(minutes=1))

        taken = await ReminderStore(db_session).claim_next(now=NOW, stale_before=STALE)

        assert taken == DueReminder(
            reminder_id=reminder_id,
            guild_id=1,
            channel_id=30,
            member_id=20,
            set_by=10,
            message="look at #77 before the release",
            set_at=NOW - timedelta(days=1),
            due_at=NOW - timedelta(minutes=1),
            failed_attempts=0,
        )

    async def test_taking_it_stamps_the_claim(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=-timedelta(minutes=1))

        await ReminderStore(db_session).claim_next(now=NOW, stale_before=STALE)
        await db_session.commit()

        assert await claimed_at(db_session, reminder_id) == NOW

    async def test_a_claim_in_flight_keeps_every_other_sender_off_it(
        self, db_session: AsyncSession
    ) -> None:
        await remember(db_session, due_in=-timedelta(minutes=1))
        store = ReminderStore(db_session)
        assert await store.claim_next(now=NOW, stale_before=STALE) is not None

        a_minute_on = NOW + timedelta(minutes=1)

        assert (
            await store.claim_next(now=a_minute_on, stale_before=STALE + timedelta(minutes=1))
            is None
        )

    async def test_a_claim_gone_stale_is_taken_over(self, db_session: AsyncSession) -> None:
        """The sender holding it stopped before it finished, and nothing else would ever send it."""
        reminder_id = await remember(db_session, due_in=-timedelta(minutes=1))
        store = ReminderStore(db_session)
        await store.claim_next(now=NOW, stale_before=STALE)

        later = NOW + timedelta(minutes=5)
        taken = await store.claim_next(now=later, stale_before=later - timedelta(minutes=5))

        assert taken is not None
        assert taken.reminder_id == reminder_id

    async def test_two_senders_asking_at_once_take_different_reminders(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`SKIP LOCKED`. The first sender's transaction is still open when the second asks, so
        its row is locked; without the clause the second would wait on it rather than move on."""
        async with db_sessionmaker() as setup:
            first = await remember(setup, due_in=-timedelta(minutes=5))
            second = await remember(setup, due_in=-timedelta(minutes=1))

        async with db_sessionmaker() as one, db_sessionmaker() as two, one.begin():
            mine = await ReminderStore(one).claim_next(now=NOW, stale_before=STALE)
            async with two.begin():
                theirs = await ReminderStore(two).claim_next(now=NOW, stale_before=STALE)

        assert mine is not None and theirs is not None
        assert (mine.reminder_id, theirs.reminder_id) == (first, second)


class TestWhatASenderLeavesBehind:
    async def test_a_failure_is_counted_and_the_claim_kept(self, db_session: AsyncSession) -> None:
        """Kept, so the next try waits out the retry window rather than coming round next tick."""
        reminder_id = await remember(db_session, due_in=-timedelta(minutes=1))
        store = ReminderStore(db_session)
        await store.claim_next(now=NOW, stale_before=STALE)

        assert await store.note_failure(reminder_id) == 1
        assert await store.note_failure(reminder_id) == 2
        assert await claimed_at(db_session, reminder_id) == NOW

    async def test_a_failure_on_one_that_has_gone_counts_nothing(
        self, db_session: AsyncSession
    ) -> None:
        assert await ReminderStore(db_session).note_failure(12345) == 0

    async def test_finishing_one_forgets_it(self, db_session: AsyncSession) -> None:
        reminder_id = await remember(db_session, due_in=-timedelta(minutes=1))
        await remember(db_session, due_in=timedelta(hours=1))

        await ReminderStore(db_session).finish(reminder_id)
        await db_session.commit()

        left = await db_session.scalars(select(Reminder.id))
        assert reminder_id not in left.all()
        assert await db_session.scalar(select(func.count()).select_from(Reminder)) == 1
