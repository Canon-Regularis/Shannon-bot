"""Writing a reminder down, the way `/remind` does. Issue #229.

Against the database, because the cap is a count of what is there and a fake would only be asked
what it was told. What is pinned: when it falls due, what becomes of the message on the way in,
and the ceiling of twenty-five a person a server, which counts reminders set for anybody.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Reminder
from shannon.services.reminders.book import MOST_WAITING, ReminderBook, TooManyRemindersError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def book(sessionmaker: async_sessionmaker[AsyncSession]) -> ReminderBook:
    return ReminderBook(sessionmaker, now=lambda: NOW)


async def set_one(
    reminders: ReminderBook,
    *,
    message: str | None = "look at the river",
    guild: int = 1,
    set_by: int = 10,
    member: int = 20,
    after: timedelta = timedelta(hours=3),
) -> datetime:
    return await reminders.add(
        guild_id=guild,
        channel_id=30,
        member_id=member,
        set_by=set_by,
        after=after,
        message=message,
    )


async def waiting(session: AsyncSession) -> int:
    counted = await session.scalar(select(func.count()).select_from(Reminder))
    return counted or 0


class TestWhenItFallsDue:
    async def test_it_is_now_plus_the_time_given(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        due = await set_one(book(db_sessionmaker), after=timedelta(days=1, hours=3, minutes=30))

        assert due == NOW + timedelta(days=1, hours=3, minutes=30)
        row = await db_session.scalar(select(Reminder))
        assert row is not None
        assert (row.set_at, row.due_at) == (NOW, due)


class TestWhatIsSaid:
    async def test_the_message_is_trimmed(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        await set_one(book(db_sessionmaker), message="   look at the river  \n")

        assert await db_session.scalar(select(Reminder.message)) == "look at the river"

    @pytest.mark.parametrize("message", [None, "", "   \n\t "])
    async def test_nothing_to_say_is_no_message(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        message: str | None,
    ) -> None:
        await set_one(book(db_sessionmaker), message=message)

        row = await db_session.scalar(select(Reminder))
        assert row is not None
        assert row.message is None

    async def test_a_message_longer_than_the_column_is_cut_rather_than_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Discord already holds the option to five hundred; this is for a caller that is not."""
        await set_one(book(db_sessionmaker), message="a" * 600)

        assert await db_session.scalar(select(Reminder.message)) == "a" * 500


class TestTheCeiling:
    async def test_the_last_one_allowed_is_allowed_and_the_next_is_not(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        reminders = book(db_sessionmaker)
        for _ in range(MOST_WAITING):
            await set_one(reminders)

        with pytest.raises(TooManyRemindersError, match="already have 25 reminders waiting"):
            await set_one(reminders)

        assert await waiting(db_session) == MOST_WAITING

    async def test_reminders_set_for_anybody_count(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The ceiling is on how much one person can have this bot say later, so a reminder they
        set for themselves and one they set for somebody else are both theirs."""
        reminders = book(db_sessionmaker)
        for index in range(MOST_WAITING):
            await set_one(reminders, member=10 if index % 2 else 20)

        with pytest.raises(TooManyRemindersError):
            await set_one(reminders, member=99)

    async def test_somebody_elses_and_another_servers_are_not_counted(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        reminders = book(db_sessionmaker)
        for _ in range(MOST_WAITING):
            await set_one(reminders, set_by=11)
        for _ in range(MOST_WAITING):
            await set_one(reminders, guild=2)

        await set_one(reminders)

        assert await waiting(db_session) == 2 * MOST_WAITING + 1
