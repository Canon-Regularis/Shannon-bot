from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, TrackedItem
from shannon.domain.enums import ActorRole
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    """A guild with the issue already synced once, which is the state updates arrive in."""
    async with registered_stack(db_engine, db_session, threads) as client:
        await deliver(client, "issues", payloads.issue_event("opened"), delivery="i0")
        yield client


async def test_an_edit_updates_the_existing_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(
        tracked,
        "issues",
        payloads.issue_event("edited", title="Locking is missing on close"),
        delivery="i1",
    )

    assert response.json()["status"] == "accepted"
    assert len(threads.created) == 1
    assert "**Issue Name:** Locking is missing on close" in threads.metadata_of(
        threads.created[0].thread_id
    )


async def test_a_title_change_renames_the_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("edited", title="Locking is missing on close"),
        delivery="i1",
    )

    assert threads.renames == [(threads.created[0].thread_id, "#12 Locking is missing on close")]


async def test_an_unchanged_title_does_not_rename(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "issues", payloads.issue_event("edited"), delivery="i1")

    assert threads.renames == []


async def test_a_priority_label_change_reaches_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("labeled", labels=[{"name": "bug"}, {"name": "priority: low"}]),
        delivery="i1",
    )

    metadata = threads.metadata_of(threads.created[0].thread_id)
    assert "**Priority:** LOW" in metadata
    assert "**Tags:** `bug`, `priority: low`" in metadata

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.priority.value == "LOW"


async def test_a_removed_label_disappears_from_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "issues", payloads.issue_event("labeled", labels=[]), delivery="i1")

    assert "**Tags:** None" in threads.metadata_of(threads.created[0].thread_id)


async def test_a_reassignment_reaches_the_metadata_and_the_database(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    await deliver(
        tracked,
        "issues",
        payloads.issue_event(
            "assigned",
            assignees=[payloads.user("monalisa", 200)],
            updated_at="2026-08-11T10:30:00Z",
        ),
        delivery="i1",
    )

    assert "**Assignees:** monalisa" in threads.metadata_of(threads.created[0].thread_id)

    rows = await db_session.scalars(
        select(ItemAssignment.github_username).where(ItemAssignment.role_type == ActorRole.ASSIGNEE)
    )
    assert sorted(rows.all()) == ["monalisa"]


async def test_a_new_assignee_is_pinged_and_the_first_is_not(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "issues",
        payloads.issue_event(
            "assigned", assignees=[payloads.user("hubot", 100), payloads.user("monalisa", 200)]
        ),
        delivery="i1",
    )

    assert len(threads.posts) == 2
    assert "monalisa" in threads.posts[1][1]
    assert "hubot" not in threads.posts[1][1]


async def test_no_update_ever_opens_a_second_thread(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    for index, action in enumerate(("edited", "labeled", "assigned", "closed")):
        await deliver(tracked, "issues", payloads.issue_event(action), delivery=f"i{index + 1}")

    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_the_metadata_message_is_edited_rather_than_reposted(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    before = await db_session.scalar(select(TrackedItem.discord_message_id))

    await deliver(tracked, "issues", payloads.issue_event("edited", title="Edited"), delivery="i1")
    db_session.expunge_all()
    after = await db_session.scalar(select(TrackedItem.discord_message_id))

    assert before is not None
    assert after == before


async def test_a_repeated_update_delivery_is_dropped(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    payload = payloads.issue_event("edited", title="Edited once")
    first = await deliver(tracked, "issues", payload, delivery="i1")
    second = await deliver(tracked, "issues", payload, delivery="i1")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.created) == 1


async def test_pull_request_sync_is_unaffected(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    """The shared sync service has to keep both kinds working side by side."""
    await deliver(tracked, "pull_request", payloads.pull_request_event("opened"), delivery="p1")

    assert len(threads.created) == 2
    channels = sorted(thread.channel_id for thread in threads.created)
    assert channels == [98, 99]
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 2


async def test_removing_an_assignee_clears_it_from_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    """Removals used to wait for some later event to correct the thread."""
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("unassigned", assignees=[], updated_at="2026-08-11T10:30:00Z"),
        delivery="i1",
    )

    assert "**Assignees:** None" in threads.metadata_of(threads.created[0].thread_id)

    rows = await db_session.scalars(
        select(ItemAssignment.github_username).where(ItemAssignment.role_type == ActorRole.ASSIGNEE)
    )
    assert rows.all() == []


async def test_removing_a_label_clears_it_from_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "issues", payloads.issue_event("unlabeled", labels=[]), delivery="i1")

    assert "**Tags:** None" in threads.metadata_of(threads.created[0].thread_id)


async def test_removing_a_priority_label_resets_the_priority(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("labeled", labels=[{"name": "priority: high"}]),
        delivery="i1",
    )
    assert "**Priority:** HIGH" in threads.metadata_of(threads.created[0].thread_id)

    await deliver(tracked, "issues", payloads.issue_event("unlabeled", labels=[]), delivery="i2")

    assert "**Priority:** UNSET" in threads.metadata_of(threads.created[0].thread_id)
