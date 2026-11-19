"""What the stores' conditional writes leave alone. Issue #268.

Each write here is guarded on what its caller last read: the thread an item pointed at, or a
delivery still being PROCESSING. Coverage ran every one of them, but no test failed when a guard
was taken out, so these pin the guards before the refactor touches the stores.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem, WebhookEvent
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.webhook_events import WebhookEventStore
from shannon.domain.enums import CardState, DeliveryStatus, ObjectType
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

# The thread the item points at now, and one it pointed at before a rebuild or a relocation.
CURRENT = 5551
LEFT = 4441
CURRENT_MESSAGE = 6661
CHANNEL = 99
SHOWN = ["bug"]

# A time no write here produces, so a write that stamps `now()` shows up as a change.
EARLIER = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def an_item(
    session: AsyncSession,
    repository: Repository,
    *,
    thread_id: int | None = CURRENT,
    locked: bool | None = None,
) -> int:
    """An item with something in every column the guarded writes touch."""
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=900_001,
        github_object_type=ObjectType.TICKET,
        github_object_number=12,
        github_url="https://github.com/orgs/o/projects/1",
        title="seeded",
        github_state=CardState.OPEN.value,
        discord_thread_id=thread_id,
        discord_message_id=CURRENT_MESSAGE if thread_id is not None else None,
        discord_thread_locked=locked,
        discord_channel_id=CHANNEL,
        shown_labels=list(SHOWN),
    )
    session.add(item)
    await session.commit()
    return item.id


async def the_row(session: AsyncSession, tracked_item_id: int) -> tuple[object, ...]:
    found = (
        await session.execute(
            select(
                TrackedItem.discord_thread_id,
                TrackedItem.discord_message_id,
                TrackedItem.discord_thread_locked,
                TrackedItem.discord_channel_id,
                TrackedItem.shown_labels,
                TrackedItem.github_state,
            ).where(TrackedItem.id == tracked_item_id)
        )
    ).one()
    return tuple(found)


async def the_column(session: AsyncSession, tracked_item_id: int, column: str) -> object:
    return await session.scalar(
        select(getattr(TrackedItem, column)).where(TrackedItem.id == tracked_item_id)
    )


@dataclass(frozen=True, slots=True)
class Writer:
    """One write guarded on the thread, the column it writes, and what lands there."""

    write: Callable[[AsyncSession, int, int], Awaitable[None]]
    column: str
    landed: object


async def note_the_lock(session: AsyncSession, item_id: int, thread_id: int) -> None:
    await ThreadPointerStore(session).note_the_lock(item_id, thread_id=thread_id, locked=True)


async def remember_channel(session: AsyncSession, item_id: int, thread_id: int) -> None:
    await ThreadPointerStore(session).remember_channel(
        item_id, thread_id=thread_id, channel_id=12345
    )


async def note_shown_labels(session: AsyncSession, item_id: int, thread_id: int) -> None:
    await ThreadPointerStore(session).note_shown_labels(
        item_id, thread_id=thread_id, shown=["feature"]
    )


async def note_label_announced(session: AsyncSession, item_id: int, thread_id: int) -> None:
    await ThreadPointerStore(session).note_label_announced(
        item_id, thread_id=thread_id, name="feature", on_it=True
    )


async def remember_card_state(session: AsyncSession, item_id: int, thread_id: int) -> None:
    await TrackedItemStore(session).remember_card_state(
        item_id, thread_id=thread_id, state=CardState.ARCHIVED
    )


WRITERS = [
    pytest.param(Writer(note_the_lock, "discord_thread_locked", True), id="note_the_lock"),
    pytest.param(Writer(remember_channel, "discord_channel_id", 12345), id="remember_channel"),
    pytest.param(Writer(note_shown_labels, "shown_labels", ["feature"]), id="note_shown_labels"),
    pytest.param(
        Writer(note_label_announced, "shown_labels", ["bug", "feature"]),
        id="note_label_announced",
    ),
    pytest.param(
        Writer(remember_card_state, "github_state", CardState.ARCHIVED.value),
        id="remember_card_state",
    ),
]


class TestAWriteAboutAThreadTheItemHasLeft:
    """A rebuild or a relocation moved the item on while the writer was talking to Discord."""

    @pytest.mark.parametrize("writer", WRITERS)
    async def test_the_row_is_left_as_it_was(
        self, db_session: AsyncSession, registered: Repository, writer: Writer
    ) -> None:
        """What the writer learned was about the old thread, so writing it would describe the new
        one wrongly."""
        item_id = await an_item(db_session, registered)
        before = await the_row(db_session, item_id)

        await writer.write(db_session, item_id, LEFT)
        await db_session.commit()

        assert await the_row(db_session, item_id) == before


class TestAWriteAboutTheThreadTheItemStillHas:
    """The other half: the guarded write is not simply a write that never lands."""

    @pytest.mark.parametrize("writer", WRITERS)
    async def test_the_write_lands(
        self, db_session: AsyncSession, registered: Repository, writer: Writer
    ) -> None:
        item_id = await an_item(db_session, registered)

        await writer.write(db_session, item_id, CURRENT)
        await db_session.commit()

        assert await the_column(db_session, item_id, writer.column) == writer.landed


async def a_delivery(session: AsyncSession, status: DeliveryStatus) -> int:
    """A delivery with something in every column an outcome writes."""
    event = WebhookEvent(
        github_delivery_id="delivery-a",
        event_type="issues",
        payload_hash="0" * 64,
        payload={"action": "opened"},
        status=status,
        attempts=2,
        processed_at=EARLIER,
        next_attempt_at=EARLIER,
        locked_until=EARLIER + timedelta(minutes=15),
        last_error="an earlier error",
    )
    session.add(event)
    await session.commit()
    return event.id


async def the_delivery(session: AsyncSession, event_id: int) -> tuple[object, ...]:
    found = (
        await session.execute(
            select(
                WebhookEvent.status,
                WebhookEvent.attempts,
                WebhookEvent.processed_at,
                WebhookEvent.next_attempt_at,
                WebhookEvent.locked_until,
                WebhookEvent.last_error,
            ).where(WebhookEvent.id == event_id)
        )
    ).one()
    return tuple(found)


async def finish(session: AsyncSession, event_id: int) -> None:
    await WebhookEventStore(session).finish(event_id, DeliveryStatus.PROCESSED)


async def retry_later(session: AsyncSession, event_id: int) -> None:
    await WebhookEventStore(session).retry_later(
        event_id, error="discord is down", delay=timedelta(minutes=1)
    )


async def give_up(session: AsyncSession, event_id: int) -> None:
    await WebhookEventStore(session).give_up(event_id, error="still down")


Outcome = Callable[[AsyncSession, int], Awaitable[None]]

# Each with the status it leaves a row it still held in.
OUTCOMES = [
    pytest.param(finish, DeliveryStatus.PROCESSED, id="finish"),
    pytest.param(retry_later, DeliveryStatus.PENDING, id="retry_later"),
    pytest.param(give_up, DeliveryStatus.FAILED, id="give_up"),
]


class TestAnOutcomeForADeliveryNoLongerHeld:
    """A worker that outlived its lease reports on a row another worker may have taken."""

    @pytest.mark.parametrize(
        "status", [DeliveryStatus.PENDING, DeliveryStatus.PROCESSED, DeliveryStatus.FAILED]
    )
    @pytest.mark.parametrize("outcome", [finish, retry_later, give_up])
    async def test_the_row_is_left_as_it_was(
        self, db_session: AsyncSession, outcome: Outcome, status: DeliveryStatus
    ) -> None:
        """Every outcome writes at least one column the seeded row disagrees with, so a write that
        got through would show here whichever it was."""
        event_id = await a_delivery(db_session, status)
        before = await the_delivery(db_session, event_id)

        await outcome(db_session, event_id)
        await db_session.commit()

        assert await the_delivery(db_session, event_id) == before


class TestAnOutcomeForADeliveryStillHeld:
    @pytest.mark.parametrize(("outcome", "lands_as"), OUTCOMES)
    async def test_the_outcome_lands_and_the_lease_is_dropped(
        self, db_session: AsyncSession, outcome: Outcome, lands_as: DeliveryStatus
    ) -> None:
        event_id = await a_delivery(db_session, DeliveryStatus.PROCESSING)

        await outcome(db_session, event_id)
        await db_session.commit()

        db_session.expire_all()
        after = await db_session.get(WebhookEvent, event_id)
        assert after is not None
        assert after.status == lands_as
        assert after.locked_until is None


async def claim(
    session: AsyncSession, item_id: int, *, thread_id: int, message_id: int, replacing: int | None
) -> tuple[int | None, int | None]:
    return await ThreadPointerStore(session).claim_thread(
        item_id, thread_id=thread_id, message_id=message_id, replacing=replacing
    )


class TestClaimingAThread:
    """`claim_thread` answers with what the row holds afterwards, whoever won.

    A caller that lost has created a thread nobody points at, and it learns which one to use from
    this answer. So the loss is answered from the row, never from the caller's own ids.
    """

    async def test_a_win_answers_with_the_callers_own_ids(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered, thread_id=LEFT)

        answer = await claim(db_session, item_id, thread_id=7777, message_id=8888, replacing=LEFT)
        await db_session.commit()

        assert answer == (7777, 8888)
        assert await the_column(db_session, item_id, "discord_thread_id") == 7777

    async def test_a_loss_answers_with_the_ids_the_winner_left(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """The caller thinks the item is still on LEFT, but another sync has moved it on."""
        item_id = await an_item(db_session, registered, thread_id=CURRENT)

        answer = await claim(db_session, item_id, thread_id=7777, message_id=8888, replacing=LEFT)
        await db_session.commit()

        assert answer == (CURRENT, CURRENT_MESSAGE)

    async def test_a_loss_leaves_the_winners_row_as_it_was(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """A different thread clears the lock and the shown labels on a win, so a loss that got
        through would reopen the winner's thread as well as replace it."""
        item_id = await an_item(db_session, registered, thread_id=CURRENT, locked=True)
        before = await the_row(db_session, item_id)

        await claim(db_session, item_id, thread_id=7777, message_id=8888, replacing=None)
        await db_session.commit()

        assert await the_row(db_session, item_id) == before

    async def test_two_first_claims_at_once_get_the_same_answer(
        self,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
    ) -> None:
        """The worker and `/pr` both created a thread for an item that had none. The second waits
        on the first's row lock, then finds the row moved and answers with the first's ids."""
        item_id = await an_item(db_session, registered, thread_id=None)

        async def the_second() -> tuple[int | None, int | None]:
            async with db_sessionmaker() as session, session.begin():
                return await claim(session, item_id, thread_id=2222, message_id=2, replacing=None)

        async with db_sessionmaker() as first, first.begin():
            won = await claim(first, item_id, thread_id=1111, message_id=1, replacing=None)
            waiting = asyncio.create_task(the_second())
            await blocked_on_a_row(db_sessionmaker, waiting)
        lost = await waiting

        assert won == lost == (1111, 1)

    async def test_an_item_that_is_not_there_answers_with_nothing(
        self, db_session: AsyncSession
    ) -> None:
        answer = await claim(db_session, 404, thread_id=7777, message_id=8888, replacing=None)

        assert answer == (None, None)
