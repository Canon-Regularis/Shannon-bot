"""Remembering which lines a thread asked for. Issue #163.

The store's half of `/track`: one choice merged into what is there in one statement, kept with the
item rather than its thread, and carried out to the board poller with the rows it already reads.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.tracking import TrackedEvent, Tracking
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

THREAD = 5551
CHANNEL = 99


async def an_item(
    session: AsyncSession,
    repository: Repository,
    *,
    kind: ObjectType = ObjectType.PR,
    object_id: int = 900_001,
    thread_id: int | None = THREAD,
) -> int:
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=object_id,
        github_object_type=kind,
        github_object_number=12,
        github_url="https://github.com/o/r/pull/12",
        title="seeded",
        discord_thread_id=thread_id,
        discord_channel_id=CHANNEL,
    )
    session.add(item)
    await session.commit()
    return item.id


async def stored(session: AsyncSession, tracked_item_id: int) -> object:
    session.expire_all()
    return await session.scalar(
        select(TrackedItem.tracking).where(TrackedItem.id == tracked_item_id)
    )


async def choose(
    session: AsyncSession, tracked_item_id: int, event: TrackedEvent, wanted: bool
) -> object:
    answered = await TrackedItemStore(session).remember_tracking(
        tracked_item_id, event=event, wanted=wanted
    )
    await session.commit()
    return answered


class TestANewItem:
    async def test_nobody_has_chosen_anything(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """The server default, so a row written by code that predates this reads the same."""
        item_id = await an_item(db_session, registered)

        assert await stored(db_session, item_id) == {}


class TestChoosing:
    async def test_one_choice_is_written_and_everything_chosen_answered(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)

        answered = await choose(db_session, item_id, TrackedEvent.LABELS, True)

        assert answered == {"labels": True}
        assert await stored(db_session, item_id) == {"labels": True}

    async def test_a_second_kind_is_kept_beside_the_first(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        await choose(db_session, item_id, TrackedEvent.COMMENTS, False)

        assert await stored(db_session, item_id) == {"labels": True, "comments": False}

    async def test_choosing_again_replaces_the_answer(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.COMMITS, True)

        await choose(db_session, item_id, TrackedEvent.COMMITS, False)

        assert await stored(db_session, item_id) == {"commits": False}

    async def test_a_key_this_release_does_not_know_is_carried_through(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """Written by a later release and read by this one after a rollback: merged into, never
        thrown away, so rolling forward again finds it where it was."""
        item_id = await an_item(db_session, registered)
        await db_session.execute(
            update(TrackedItem).where(TrackedItem.id == item_id).values(tracking={"later": True})
        )
        await db_session.commit()

        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        assert await stored(db_session, item_id) == {"later": True, "labels": True}

    async def test_an_item_that_has_gone_answers_nothing(self, db_session: AsyncSession) -> None:
        assert await choose(db_session, 999_999, TrackedEvent.LABELS, True) is None

    async def test_two_people_choosing_different_kinds_at_once_both_land(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """One statement each, so the second re-reads the row the first wrote rather than a copy
        it read before the first committed."""
        async with db_sessionmaker() as setup:
            item_id = await an_item(setup, registered)

        async def the_other_choice() -> None:
            async with db_sessionmaker() as two:
                await choose(two, item_id, TrackedEvent.COMMITS, True)

        async with db_sessionmaker() as one, one.begin():
            await TrackedItemStore(one).remember_tracking(
                item_id, event=TrackedEvent.LABELS, wanted=True
            )
            waiting = asyncio.create_task(the_other_choice())
            await blocked_on_a_row(db_sessionmaker, waiting)
        await waiting

        async with db_sessionmaker() as check:
            assert await stored(check, item_id) == {"labels": True, "commits": True}


class TestItBelongsToTheItem:
    """Not cleared with the thread pointer, so a rebuilt or moved thread keeps what was chosen."""

    async def test_letting_go_of_a_dead_thread_keeps_it(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        assert await ThreadPointerStore(db_session).forget_thread(item_id, dead_thread_id=THREAD)
        await db_session.commit()

        assert await stored(db_session, item_id) == {"labels": True}

    async def test_a_replacement_thread_keeps_it(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        await ThreadPointerStore(db_session).claim_thread(
            item_id, thread_id=7777, message_id=1, replacing=THREAD
        )
        await db_session.commit()

        assert await stored(db_session, item_id) == {"labels": True}

    async def test_a_channel_that_went_keeps_it(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        await ThreadPointerStore(db_session).forget_channel(CHANNEL)
        await db_session.commit()

        assert await stored(db_session, item_id) == {"labels": True}


class TestFindingItByItsThread:
    async def test_the_item_is_found_by_its_thread(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)

        found = await TrackedItemStore(db_session).get_by_thread(THREAD)

        assert found is not None
        assert found.id == item_id

    async def test_it_can_be_held_while_a_choice_is_made(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """So whether a kind was already on is read against what is about to be written."""
        async with db_sessionmaker() as setup:
            item_id = await an_item(setup, registered)

        async def a_write_behind_it() -> None:
            async with db_sessionmaker() as two:
                await choose(two, item_id, TrackedEvent.COMMITS, True)

        async with db_sessionmaker() as one, one.begin():
            assert await TrackedItemStore(one).get_by_thread(THREAD, lock=True) is not None
            waiting = asyncio.create_task(a_write_behind_it())
            await blocked_on_a_row(db_sessionmaker, waiting)
        await waiting


class TestTheBoardPollerSeesIt:
    async def test_every_item_a_card_could_wrap_carries_it(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        await choose(db_session, item_id, TrackedEvent.STATE, False)

        rows = await TrackedItemStore(db_session).board_state(repository_id=registered.id)

        assert rows[(ObjectType.PR, 900_001)].tracking == Tracking(
            off=frozenset({TrackedEvent.STATE})
        )

    async def test_every_ticket_thread_carries_it(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered, kind=ObjectType.TICKET)
        await choose(db_session, item_id, TrackedEvent.LABELS, True)

        [ticket] = await TrackedItemStore(db_session).ticket_threads(repository_id=registered.id)

        assert ticket.tracking == Tracking(on=frozenset({TrackedEvent.LABELS}))
