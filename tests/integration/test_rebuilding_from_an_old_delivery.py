"""Losing a thread must not also lose what the item had become.

An item with no thread is never treated as stale, or it would never get one. That bypass means
"build the thread anyway" and nothing more: an old payload must not put back a title that has
since changed, nor swap the people for whoever was on it at the time.

The pointer is cleared often enough for this to matter. The note mirror lets go of a thread
somebody deleted, and an item whose first thread creation failed has a committed row and no
thread.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.pull_request import parse_pull_request_event
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import build_stack

pytestmark = pytest.mark.integration

BEFORE = "2026-08-10T11:00:00Z"
STALE = "2026-08-10T11:30:00Z"
AFTER = "2026-08-10T12:00:00Z"


def pr(action: str, **overrides):
    snapshot = parse_pull_request_event(action, payloads.pull_request_event(action, **overrides))
    assert snapshot is not None
    return snapshot


async def stored(session: AsyncSession) -> tuple[str, int | None, list[str]]:
    session.expunge_all()
    item = await session.scalar(select(TrackedItem))
    rows = await session.scalars(
        select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
    )
    return item.title, item.discord_thread_id, sorted(row.github_username for row in rows)


async def forget_the_thread(container, session: AsyncSession) -> None:
    """What the note mirror does when a comment finds the thread gone."""
    item = await session.scalar(select(TrackedItem))
    async with container.sessionmaker() as writing, writing.begin():
        await ThreadPointerStore(writing).forget_thread(
            item.id, dead_thread_id=item.discord_thread_id
        )


async def test_an_old_delivery_rebuilds_the_thread_without_reverting_the_item(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    sync = container.pr_sync

    await sync.sync(pr("opened", updated_at=BEFORE, title="OLD title"))
    await sync.sync(pr("edited", updated_at=AFTER, title="NEW title"))
    await forget_the_thread(container, db_session)

    posts_before = len(threads.posts)
    await sync.sync(
        pr(
            "edited",
            updated_at=STALE,
            title="OLD title",
            requested_reviewers=[payloads.user("ghost-reviewer", 900)],
        )
    )

    title, thread_id, reviewers = await stored(db_session)
    assert title == "NEW title", "an old delivery put back a title that had already changed"
    assert reviewers == ["monalisa"], "an old delivery decided who was on the item"
    assert thread_id is not None, "the item was left with no thread, which it never recovers from"
    assert threads.posts[posts_before:] == [], "somebody was pinged by a delivery from the past"


async def test_a_current_delivery_still_rebuilds_and_applies(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The guard must not cost the ordinary rebuild anything."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    sync = container.pr_sync

    await sync.sync(pr("opened", updated_at=BEFORE, title="OLD title"))
    await forget_the_thread(container, db_session)

    await sync.sync(pr("edited", updated_at=AFTER, title="NEW title"))

    title, thread_id, _ = await stored(db_session)
    assert (title, thread_id is not None) == ("NEW title", True)


async def test_an_item_that_never_had_a_thread_is_still_built_from_its_own_delivery(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Nothing is stored yet, so there is nothing for the delivery to be older than."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)

    await container.pr_sync.sync(pr("opened", updated_at=BEFORE, title="OLD title"))

    title, thread_id, reviewers = await stored(db_session)
    assert (title, thread_id is not None, reviewers) == ("OLD title", True, ["monalisa"])
