from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.services.sync.items import ItemSyncService
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

EARLY = "2026-08-11T09:00:00Z"
LATE = "2026-08-11T18:00:00Z"


async def test_an_out_of_order_delivery_does_not_revert_the_title(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """GitHub does not guarantee webhook ordering, and retries arrive late.

    A stale event carries the old title, and applying it would undo a rename that already
    reached the thread.
    """
    await issue_service.sync(issue_event("opened", title="Old title", updated_at=EARLY))
    await issue_service.sync(issue_event("edited", title="New title", updated_at=LATE))

    await issue_service.sync(issue_event("labeled", title="Old title", updated_at=EARLY))

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.title == "New title"
    assert "**Issue Name:** New title" in threads.metadata_of(threads.created[0].thread_id)


async def test_an_out_of_order_delivery_does_not_revert_the_state(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    """Reopening an issue then receiving a late close event must not close it again."""
    await issue_service.sync(issue_event("opened", updated_at=EARLY))
    await issue_service.sync(issue_event("reopened", state="open", updated_at=LATE, closed_at=None))

    await issue_service.sync(
        issue_event("closed", state="closed", updated_at=EARLY, closed_at=EARLY)
    )

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_state == "open"


async def test_a_delivery_with_the_same_timestamp_still_applies(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    """Several changes inside one second share a timestamp, and all of them are real."""
    await issue_service.sync(issue_event("opened", title="First", updated_at=LATE))

    await issue_service.sync(issue_event("labeled", title="Second", updated_at=LATE))

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.title == "Second"


async def test_a_snapshot_without_a_timestamp_still_applies(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    """No timestamp means no evidence of staleness, so the event is trusted."""
    await issue_service.sync(issue_event("opened", title="First", updated_at=LATE))

    await issue_service.sync(issue_event("edited", title="Second", updated_at=None))

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.title == "Second"


async def test_a_stale_delivery_before_a_thread_exists_is_still_synced(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """Skipping here would mean the item never gets a thread at all."""
    result = await issue_service.sync(issue_event("opened", updated_at=EARLY))

    assert result is not None
    assert len(threads.created) == 1
