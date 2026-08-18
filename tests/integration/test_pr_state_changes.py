from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import Status
from shannon.services.sync.items import ItemSyncService
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

MERGED = {"state": "closed", "merged": True, "merged_at": "2026-08-10T13:00:00Z"}


async def test_a_new_pull_request_shows_as_open(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    result = await sync_service.sync(pr_event("opened"))

    assert result is not None
    assert "**State:** Open" in threads.metadata_of(result.thread_id)


async def test_closing_updates_the_existing_thread(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    opened = await sync_service.sync(pr_event("opened"))
    closed = await sync_service.sync(pr_event("closed", state="closed"))

    assert opened is not None and closed is not None
    assert closed.thread_id == opened.thread_id
    assert closed.created is False
    assert len(threads.created) == 1
    assert "**State:** Closed" in threads.metadata_of(closed.thread_id)


async def test_a_merged_pull_request_says_merged(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await sync_service.sync(pr_event("opened"))
    merged = await sync_service.sync(pr_event("closed", **MERGED))

    assert merged is not None
    assert "**State:** Merged" in threads.metadata_of(merged.thread_id)


async def test_reopening_goes_back_to_open(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("closed", state="closed"))
    reopened = await sync_service.sync(pr_event("reopened", state="open"))

    assert reopened is not None
    assert "**State:** Open" in threads.metadata_of(reopened.thread_id)
    assert len(threads.created) == 1


async def test_the_state_is_persisted(
    registered: Repository,
    sync_service: ItemSyncService,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("closed", **MERGED))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_state == "merged"


async def test_closing_does_not_touch_the_workflow_status(
    registered: Repository,
    sync_service: ItemSyncService,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """Status is what /SET_DONE moves in MVP 3. Closing a pull request is not that."""
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("closed", state="closed"))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.status == Status.NOT_REVIEWED


async def test_a_closed_thread_is_left_unlocked(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    """Issues lock when they close. Pull requests do not, until /SET_DONE arrives in MVP 3."""
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("closed", state="closed"))

    assert threads.locks == []
