"""Reading and changing which lines a thread is told. Issue #163.

The service `/track` drives: it finds the item by its thread, reads what that thread chose, and
writes one more choice down, against the database.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.tracking import TrackedEvent, Tracking
from shannon.services.tracking import NOT_AN_ITEM_THREAD, ThreadTracking
from shannon.services.workflow import NotAnItemThreadError
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

THREAD = 6661
ALICE = 555


async def an_item(
    sessionmaker: async_sessionmaker[AsyncSession],
    repository: Repository,
    *,
    kind: ObjectType = ObjectType.PR,
    thread_id: int = THREAD,
) -> int:
    async with sessionmaker() as session:
        item = TrackedItem(
            repository_id=repository.id,
            github_object_id=900_001,
            github_object_type=kind,
            github_object_number=12,
            github_url="https://github.com/o/r/pull/12",
            title="seeded",
            discord_thread_id=thread_id,
        )
        session.add(item)
        await session.commit()
        return item.id


async def stored(sessionmaker: async_sessionmaker[AsyncSession], tracked_item_id: int) -> object:
    async with sessionmaker() as session:
        return await session.scalar(
            select(TrackedItem.tracking).where(TrackedItem.id == tracked_item_id)
        )


class TestReading:
    async def test_a_thread_nobody_has_touched_is_told_the_defaults(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        await an_item(db_sessionmaker, registered)

        lines = await ThreadTracking(db_sessionmaker).told(thread_id=THREAD)

        assert (lines.kind, lines.tracking, lines.was) == (ObjectType.PR, Tracking(), None)

    async def test_a_board_cards_thread_can_be_read_too(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        await an_item(db_sessionmaker, registered, kind=ObjectType.TICKET)

        lines = await ThreadTracking(db_sessionmaker).told(thread_id=THREAD)

        assert lines.kind is ObjectType.TICKET

    async def test_a_thread_that_is_no_items_is_refused_by_name(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(NotAnItemThreadError) as refused:
            await ThreadTracking(db_sessionmaker).told(thread_id=12345)

        assert refused.value.message == NOT_AN_ITEM_THREAD


class TestChoosing:
    async def test_a_choice_is_written_and_read_back(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        item_id = await an_item(db_sessionmaker, registered)
        tracking = ThreadTracking(db_sessionmaker)

        chosen = await tracking.choose(
            thread_id=THREAD, event=TrackedEvent.LABELS, wanted=True, by=ALICE
        )

        assert chosen.was is False
        assert chosen.tracking.wants(TrackedEvent.LABELS) is True
        assert (await tracking.told(thread_id=THREAD)).tracking == chosen.tracking
        assert await stored(db_sessionmaker, item_id) == {"labels": True}

    async def test_a_kind_already_so_is_still_written_down(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """So the thread keeps its answer if a later release changes the default."""
        item_id = await an_item(db_sessionmaker, registered)

        chosen = await ThreadTracking(db_sessionmaker).choose(
            thread_id=THREAD, event=TrackedEvent.COMMENTS, wanted=True, by=ALICE
        )

        assert chosen.was is True
        assert await stored(db_sessionmaker, item_id) == {"comments": True}

    async def test_two_people_choosing_at_once_the_second_reads_what_the_first_wrote(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Both turned labels on. The second is told it was already on, because the row is held
        from the read to the write, so it is read only once the first choice has landed."""
        item_id = await an_item(db_sessionmaker, registered)

        async with db_sessionmaker() as first, first.begin():
            store = TrackedItemStore(first)
            assert await store.get_by_thread(THREAD, lock=True) is not None
            second = asyncio.create_task(
                ThreadTracking(db_sessionmaker).choose(
                    thread_id=THREAD, event=TrackedEvent.LABELS, wanted=True, by=ALICE
                )
            )
            await blocked_on_a_row(db_sessionmaker, second)
            await store.remember_tracking(item_id, event=TrackedEvent.LABELS, wanted=True)

        assert (await second).was is True, "it read the row before the first choice landed"

    async def test_a_kind_the_item_never_gets_writes_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """An issue has no commits, so there is nothing for the choice to be about."""
        item_id = await an_item(db_sessionmaker, registered, kind=ObjectType.ISSUE)

        chosen = await ThreadTracking(db_sessionmaker).choose(
            thread_id=THREAD, event=TrackedEvent.COMMITS, wanted=True, by=ALICE
        )

        assert (chosen.kind, chosen.was) == (ObjectType.ISSUE, None)
        assert await stored(db_sessionmaker, item_id) == {}

    async def test_a_thread_that_is_no_items_writes_nothing_and_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(NotAnItemThreadError):
            await ThreadTracking(db_sessionmaker).choose(
                thread_id=12345, event=TrackedEvent.LABELS, wanted=True, by=ALICE
            )

    async def test_a_choice_outlives_the_thread_it_was_made_in(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Discord deleted the thread and a new one was built for the item: still on."""
        item_id = await an_item(db_sessionmaker, registered)
        tracking = ThreadTracking(db_sessionmaker)
        await tracking.choose(thread_id=THREAD, event=TrackedEvent.LABELS, wanted=True, by=ALICE)
        async with db_sessionmaker() as session:
            pointers = ThreadPointerStore(session)
            await pointers.forget_thread(item_id, dead_thread_id=THREAD)
            await pointers.claim_thread(item_id, thread_id=7777, message_id=1, replacing=None)
            await session.commit()

        lines = await tracking.told(thread_id=7777)

        assert lines.tracking.wants(TrackedEvent.LABELS) is True
