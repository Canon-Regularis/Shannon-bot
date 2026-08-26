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
from shannon.github.webhooks.issues import parse_issue_event
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


def issue(action: str, **overrides):
    snapshot = parse_issue_event(action, payloads.issue_event(action, **overrides))
    assert snapshot is not None
    return snapshot


def renamed_to(full_name: str, **overrides):
    """The same pull request, after GitHub has moved the repository under it.

    Built by hand because the payload helper fills the repository in for itself: an event carries
    the name as of the moment it was sent, which is the whole point here.
    """
    owner, _, name = full_name.partition("/")
    payload = payloads.pull_request_event("edited", **overrides)
    payload["repository"] = payloads.repository(
        name=name,
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        owner=payloads.user(owner, 80922799),
    )
    snapshot = parse_pull_request_event("edited", payload)
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


async def test_an_old_delivery_does_not_put_the_repositorys_old_name_back(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The one field the rebuild bypass was still believing.

    `_resolve` turns a stale delivery away, which is what keeps the name safe in the ordinary
    case, so the test that pins that never reaches `_write`. This path does reach it, on purpose,
    because a thread that has gone has to be rebuilt however old the delivery is, and the rename
    was being followed above the guard that refuses the rest of the payload. A repository renamed
    or transferred on GitHub had its stored name and URL rolled back to whatever it was called
    before, and nothing rewrites that row until the next current delivery.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    sync = container.pr_sync

    await sync.sync(pr("opened", updated_at=BEFORE))
    await sync.sync(renamed_to("big-corp/moved", updated_at=AFTER))
    db_session.expunge_all()
    moved = await db_session.scalar(select(Repository))
    assert moved.repo_name == "big-corp/moved", "the rename was never followed in the first place"

    await forget_the_thread(container, db_session)
    await sync.sync(pr("edited", updated_at=STALE))

    db_session.expunge_all()
    now = await db_session.scalar(select(Repository))
    assert now.repo_name == moved.repo_name, "a delivery from the past renamed the repository back"
    assert now.repo_url == moved.repo_url


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


async def test_a_closed_issue_rebuilt_from_an_open_delivery_still_gets_a_shut_thread(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The lock was the one thing still decided from the payload this path refuses to believe.

    An issue closes, which locks its thread. Somebody deletes the thread. A delivery from before
    the close is retried and rebuilds it, and the payload it carries says the issue is open. The
    title, the state, the status and the people are all taken from the row, because this path
    refuses that payload about every one of them; the lock was read off the payload, so the
    replacement thread came back open on an issue the row records as closed and DONE.

    Nothing else shuts it. `IssuePolicy.locked` is the only thing that locks an issue's thread,
    and it is reached once per delivery, so the next current event would have to arrive to put it
    right, and a closed issue has no reason to send one.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    sync = container.issue_sync

    await sync.sync(issue("opened", updated_at=BEFORE))
    await sync.sync(issue("closed", state="closed", updated_at=AFTER))
    closed_thread = threads.created[-1].thread_id
    assert threads.threads[closed_thread].locked is True, "the close did not lock it"

    await forget_the_thread(container, db_session)
    await sync.sync(issue("edited", updated_at=STALE))

    rebuilt = threads.created[-1].thread_id
    assert rebuilt != closed_thread, "nothing was rebuilt, so this proves nothing"
    assert threads.threads[rebuilt].locked is True, "a closed issue was given an open thread"


async def test_a_rebuilt_block_says_what_the_row_says_rather_than_what_the_payload_says(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The block was rendered from the very payload this path refuses to believe.

    A merged pull request whose thread somebody deleted was rebuilt saying `State: Open`, and a
    closed issue was rebuilt with `State: Open` directly above `Status: DONE`, a block that
    contradicts itself and the row it was built from in the same transaction.

    The stale block is accepted elsewhere on the grounds that the next delivery corrects it, and
    that is what fails here: a merged pull request and a closed issue send no more events, so it
    was not a window but the last thing the thread ever said.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    sync = container.pr_sync

    await sync.sync(pr("opened", updated_at=BEFORE, title="OLD title"))
    await sync.sync(pr("closed", state="closed", merged=True, updated_at=AFTER, title="NEW title"))
    await forget_the_thread(container, db_session)

    await sync.sync(pr("edited", updated_at=STALE, title="OLD title"))

    rebuilt = threads.created[-1]
    block = threads.metadata_of(rebuilt.thread_id)
    assert "**State:** Merged" in block, f"the rebuilt block reported the stale payload: {block}"
    assert "**PR Name:** NEW title" in block
    assert rebuilt.name == "#7 NEW title", "the thread was named from the stale payload"
