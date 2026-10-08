"""Writing a reminder down when `/remind` is run. Issue #229."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import REMINDER_MESSAGE_WIDTH
from shannon.db.stores.reminders import ReminderStore
from shannon.domain.errors import ShannonError

logger = logging.getLogger(__name__)

# How many reminders one person may have waiting in one server, whoever they are for. Reminding
# somebody else already takes a role; this is the ceiling under everybody, so nobody can have this
# bot say an unbounded amount later, and the table stays a queue rather than a store.
MOST_WAITING = 25


class TooManyRemindersError(ShannonError):
    """Somebody already has as many reminders waiting in a server as one person may."""


class ReminderBook:
    """Writes reminders down, at most `MOST_WAITING` a person a server."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._now = now

    async def add(
        self,
        *,
        guild_id: int,
        channel_id: int,
        member_id: int,
        set_by: int,
        after: timedelta,
        message: str | None,
    ) -> datetime:
        """Write one down, answering when it falls due.

        Counted and written in the one transaction, so the cap is read against what is there when
        this writes. It is a soft ceiling rather than a hard one: two commands from one person in
        the same moment can each find room for one more, which costs a twenty-sixth reminder and
        nothing worse.

        The message is trimmed, and one that was nothing but whitespace is no message at all.
        Discord holds the option to the column's width already; the cut here is for a caller that
        is not Discord.
        """
        set_at = self._now()
        due_at = set_at + after
        said = (message or "").strip()[:REMINDER_MESSAGE_WIDTH] or None
        async with self._sessionmaker() as session, session.begin():
            store = ReminderStore(session)
            if await store.waiting_from(guild_id=guild_id, set_by=set_by) >= MOST_WAITING:
                raise TooManyRemindersError(
                    f"You already have {MOST_WAITING} reminders waiting in this server, which is "
                    "as many as one person may. One has to go off before you can set another."
                )
            reminder_id = await store.add(
                guild_id=guild_id,
                channel_id=channel_id,
                member_id=member_id,
                set_by=set_by,
                message=said,
                set_at=set_at,
                due_at=due_at,
            )
        # Ids and times only. The message is somebody's words, and a log is read by people the
        # channel it was said in was not.
        logger.info(
            "reminder %s set by %s for %s in channel %s, due %s",
            reminder_id,
            set_by,
            member_id,
            channel_id,
            due_at.isoformat(),
        )
        return due_at
