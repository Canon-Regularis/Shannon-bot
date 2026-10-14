"""When each item with a thread last had its row written. Issue #185.

What `/refresh` orders its redraws by, so a run that stops early leaves the next one starting with
the threads it did not reach. The row's own clock, because a redraw rewrites the row and never
moves GitHub's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from tests.support.db import register_repository

pytestmark = pytest.mark.integration

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


async def an_item(
    session: AsyncSession,
    repository: Repository,
    *,
    object_id: int,
    kind: ObjectType = ObjectType.PR,
    thread_id: int | None,
) -> int:
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=object_id,
        github_object_type=kind,
        github_object_number=object_id % 1000,
        github_url=f"https://github.com/o/r/pull/{object_id % 1000}",
        title="seeded",
        discord_thread_id=thread_id,
    )
    session.add(item)
    await session.commit()
    return item.id


async def last_written(
    session: AsyncSession, repository: Repository, kind: ObjectType = ObjectType.PR
) -> dict[int, datetime]:
    return await TrackedItemStore(session).last_written(
        repository_id=repository.id, object_type=kind
    )


async def test_only_items_with_a_thread_are_answered_for(
    registered: Repository, db_session: AsyncSession
) -> None:
    """One with no thread has nothing to redraw: `/refresh` opens it instead."""
    await an_item(db_session, registered, object_id=900_001, thread_id=5001)
    await an_item(db_session, registered, object_id=900_002, thread_id=None)

    assert set(await last_written(db_session, registered)) == {900_001}


async def test_only_this_repository_and_this_kind(
    registered: Repository, db_session: AsyncSession
) -> None:
    other = await register_repository(
        db_session, guild_id=2, channel_id=77, github_repo_id=424242, repo_name="acme/other"
    )
    await an_item(db_session, registered, object_id=900_001, thread_id=5001)
    await an_item(db_session, registered, object_id=900_002, kind=ObjectType.ISSUE, thread_id=5002)
    await an_item(db_session, other, object_id=900_003, thread_id=5003)

    assert set(await last_written(db_session, registered)) == {900_001}
    assert set(await last_written(db_session, registered, ObjectType.ISSUE)) == {900_002}


async def test_the_write_a_redraw_makes_moves_it_forward(
    registered: Repository, db_session: AsyncSession
) -> None:
    """A redraw that lands records its thread again, swapping the pointer for itself, and that
    is an UPDATE like any other: the stamp moves, so the thread goes to the back of the queue."""
    item_id = await an_item(db_session, registered, object_id=900_001, thread_id=5001)
    await db_session.execute(
        update(TrackedItem).where(TrackedItem.id == item_id).values(updated_at=LONG_AGO)
    )
    await db_session.commit()
    assert (await last_written(db_session, registered))[900_001] == LONG_AGO, "arranged wrong"

    await ThreadPointerStore(db_session).claim_thread(
        item_id, thread_id=5001, message_id=1, replacing=5001
    )
    await db_session.commit()

    written = await last_written(db_session, registered)
    assert written[900_001] > LONG_AGO
    db_session.expire_all()
    assert written[900_001] == await db_session.scalar(
        select(TrackedItem.updated_at).where(TrackedItem.id == item_id)
    )
