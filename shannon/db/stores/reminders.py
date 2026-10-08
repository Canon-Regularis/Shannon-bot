"""The reminders waiting to go off. Issue #229.

Written by `/remind` and read back by the sender when each falls due. A row is the whole of a
reminder that is still owed: it is deleted once it has gone out or been given up on, so nothing
here is ever a record of the past.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Reminder


@dataclass(frozen=True, slots=True)
class DueReminder:
    """One reminder a sender has taken, with everything its message needs."""

    reminder_id: int
    guild_id: int
    channel_id: int
    member_id: int
    set_by: int
    message: str | None
    set_at: datetime
    due_at: datetime
    failed_attempts: int


class ReminderStore:
    """Data access for the reminder queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        guild_id: int,
        channel_id: int,
        member_id: int,
        set_by: int,
        message: str | None,
        set_at: datetime,
        due_at: datetime,
    ) -> int:
        """Write a reminder down, answering its id."""
        added = await self._session.execute(
            insert(Reminder)
            .values(
                discord_guild_id=guild_id,
                discord_channel_id=channel_id,
                discord_user_id=member_id,
                set_by_discord_user_id=set_by,
                message=message,
                set_at=set_at,
                due_at=due_at,
            )
            .returning(Reminder.id)
        )
        return added.scalar_one()

    async def waiting_from(self, *, guild_id: int, set_by: int) -> int:
        """How many reminders this person has set in this server that have not gone off yet.

        Whoever they are for, themselves included: the cap this serves is on how much one person
        can have this bot say later, not on how often one person is rung.
        """
        counted = await self._session.scalar(
            select(func.count())
            .select_from(Reminder)
            .where(
                Reminder.discord_guild_id == guild_id,
                Reminder.set_by_discord_user_id == set_by,
            )
        )
        return counted or 0

    async def claim_next(self, *, now: datetime, stale_before: datetime) -> DueReminder | None:
        """Take the reminder that fell due first, or None when nothing is due.

        Selected and stamped in one statement, so nothing can slip between the two, and `SKIP
        LOCKED` so a second sender takes a different row rather than waiting on this one. A claim
        stamped before `stale_before` belongs to a sender that stopped before finishing, and is
        taken over; one stamped since is somebody else's send in flight, and is left alone.

        The application's clock throughout, which the caller passes in. One process sends, and a
        test can move the clock it holds.
        """
        eligible = (
            select(Reminder.id)
            .where(
                Reminder.due_at <= now,
                or_(Reminder.claimed_at.is_(None), Reminder.claimed_at <= stale_before),
            )
            .order_by(Reminder.due_at, Reminder.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        claimed = (
            await self._session.scalars(
                update(Reminder)
                .where(Reminder.id.in_(eligible))
                .values(claimed_at=now)
                .returning(Reminder)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if claimed is None:
            return None
        return DueReminder(
            reminder_id=claimed.id,
            guild_id=claimed.discord_guild_id,
            channel_id=claimed.discord_channel_id,
            member_id=claimed.discord_user_id,
            set_by=claimed.set_by_discord_user_id,
            message=claimed.message,
            set_at=claimed.set_at,
            due_at=claimed.due_at,
            failed_attempts=claimed.failed_attempts,
        )

    async def note_failure(self, reminder_id: int) -> int:
        """Count a send Discord would not take, answering how many there have been.

        The claim is kept, so the next try waits out the retry window rather than coming round on
        the very next tick. 0 for a reminder that has gone.
        """
        counted = await self._session.scalar(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(failed_attempts=Reminder.failed_attempts + 1)
            .returning(Reminder.failed_attempts)
        )
        return counted or 0

    async def finish(self, reminder_id: int) -> None:
        """Forget a reminder: it has gone out, or it never will."""
        await self._session.execute(delete(Reminder).where(Reminder.id == reminder_id))
