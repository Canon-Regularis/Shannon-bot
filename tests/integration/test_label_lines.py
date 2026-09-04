"""A label going on or coming off says so in the thread.

The metadata block already lists every label and is rewritten on every delivery, so the state was
never wrong. What was missing is that a Discord edit is silent: it posts no message, notifies
nobody and does not bump the thread, so tagging an item looked from the channel exactly like
nothing happening. Reported as issue #62 after watching it happen in a real server.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import MirroredNote, Repository, WebhookEvent
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration


def lines(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if body.startswith("Tag ")]


def labelled(action: str, name: str) -> dict:
    """An issue event carrying the label that moved, the way GitHub sends one.

    The payload helper builds only the inner issue, so the top-level `label` is added here. That
    is the whole of what separates these two actions from every other: one delivery, one label,
    named where nothing else names it.
    """
    payload = payloads.issue_event(action, labels=[{"name": name}])
    payload["label"] = {"name": name, "color": "d73a4a"}
    return payload


async def expire_the_lease(container, delivery: str) -> None:
    """Put the delivery where a dead worker's rows end up: leased, but not for much longer.

    This is the redelivery that actually happens. The queue is at-least-once because a delivery
    whose status could not be written stays leased and comes back, so the handler runs again from
    the top with everything it did the first time already done.
    """
    async with container.sessionmaker() as session, session.begin():
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.github_delivery_id == delivery)
            .values(locked_until=text("now() - interval '1 hour'"))
        )


async def with_a_thread(client, container) -> None:
    await post(client, "issues", payloads.issue_event("opened"), delivery="issue-1")
    await container.worker.run_once()


async def test_a_label_going_on_says_so(registered: Repository, db_engine: AsyncEngine) -> None:
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("labeled", "high priority"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["Tag `high priority` added."]


async def test_a_label_coming_off_says_so(registered: Repository, db_engine: AsyncEngine) -> None:
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("unlabeled", "wontfix"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["Tag `wontfix` removed."]


async def test_the_same_delivery_handled_twice_says_it_once(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The queue is at-least-once by design: a delivery whose status could not be written comes
    back when its lease runs out and is handled again from the top. Saying it twice would be
    worse than the silence this replaced, because a thread reading two identical lines invites
    the reader to look for two changes.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        payload = labelled("labeled", "bug")
        await post(client, "issues", payload, delivery="tag-1")
        await container.worker.run_once()

        await expire_the_lease(container, "tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["Tag `bug` added."]
    held = await db_session.scalar(select(func.count()).select_from(MirroredNote))
    assert held == 1, "the claim that makes it say it once was not taken"


async def test_an_event_that_moves_no_label_says_nothing(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """An item opened with four labels is one delivery carrying four names and no move. Only the
    two actions GitHub sends one label with have anything to announce."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(
            client,
            "issues",
            payloads.issue_event("edited", labels=[{"name": "bug"}, {"name": "docs"}]),
            delivery="edit-1",
        )
        await container.worker.run_once()

    assert lines(threads) == []


async def test_the_block_is_still_written_on_the_same_delivery(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """The line is an announcement, not a replacement. Whoever reads the thread a week later
    scrolls to the block, and it has to be current."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("labeled", "needs design"), delivery="tag-1")
        await container.worker.run_once()

    thread_id = threads.created[0].thread_id
    assert "needs design" in threads.metadata_of(thread_id), "the block did not keep up"
    assert lines(threads) == ["Tag `needs design` added."]
