"""A label going on or coming off says so in the thread.

The metadata block already lists every label and is rewritten on every delivery, so the state was
never wrong. What was missing is that a Discord edit is silent: it posts no message, notifies
nobody and does not bump the thread, so tagging an item looked from the channel exactly like
nothing happening. Reported as issue #62 after watching it happen in a real server.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote, Repository, WebhookEvent
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_label_change
from shannon.github.webhooks.issues import parse_issue_event
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.label_lines import LabelLine
from shannon.services.sync.policies import IssuePolicy
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
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
) -> None:
    """The queue is at-least-once by design: a delivery whose status could not be written comes
    back when its lease runs out and is handled again from the top. Saying it twice would be
    worse than the silence this replaced, because a thread reading two identical lines invites
    the reader to look for two changes.

    The handler is called twice rather than the worker being made to replay, because a handled
    delivery is in a terminal state and expiring its lease does not bring it back. Driving the
    queue instead looked like this test and was not: nothing re-entered the announcer at all, and
    it passed on the row count alone.
    """
    threads = FakeThreadGateway()
    announcer = LabelLine(db_sessionmaker, threads, render=format_label_change)
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)
    payload = labelled("labeled", "bug")

    await handle("labeled", payload, 900_002)
    await handle("labeled", payload, 900_002)

    assert lines(threads) == ["Tag `bug` added."]
    held = await db_session.scalar(select(func.count()).select_from(MirroredNote))
    assert held == 1, "the claim that makes it say it once was not taken"


async def test_a_refused_post_gives_the_claim_back_so_the_retry_says_it(
    registered: Repository, db_sessionmaker: async_sessionmaker
) -> None:
    """A claim taken and not given back is worse than saying nothing: the retry reads it as
    already said and the line is lost for good, with the delivery reported handled."""
    threads = _RefusesTheFirstPost()
    announcer = LabelLine(db_sessionmaker, threads, render=format_label_change)
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)
    payload = labelled("labeled", "bug")

    with pytest.raises(DiscordGatewayError):
        await handle("labeled", payload, 900_002)
    assert lines(threads) == [], "it says nothing when the post was refused"

    await handle("labeled", payload, 900_002)

    assert lines(threads) == ["Tag `bug` added."], "the claim was never given back"


async def test_a_claim_that_cannot_be_given_back_is_said_loudly(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both halves failing at once is rare and unrecoverable, so the one thing owed is a line
    saying which row to remove to have it said again."""
    threads = _RefusesTheFirstPost()
    announcer = LabelLine(_FailsToGiveItBack(db_sessionmaker), threads, render=format_label_change)
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)

    with caplog.at_level(logging.ERROR), pytest.raises(DiscordGatewayError):
        await handle("labeled", labelled("labeled", "bug"), 900_002)

    assert "mirrored_notes" in caplog.text, f"it went quiet about it: {caplog.text}"


class _RefusesTheFirstPost(FakeThreadGateway):
    """Discord refusing the line while accepting everything before it, which is the ordinary
    shape of a bad moment: the block landed and the announcement did not."""

    def __init__(self) -> None:
        super().__init__()
        self._refusals = 1

    async def post(self, **kwargs) -> None:
        if self._refusals:
            self._refusals -= 1
            raise DiscordGatewayError("Discord refused to post the line")
        await super().post(**kwargs)


class _FailsToGiveItBack:
    """A sessionmaker that hands out working sessions until the claim is being released."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker
        self._left = 1

    def __call__(self):
        if self._left:
            self._left -= 1
            return self._sessionmaker()
        raise RuntimeError("the database went away")


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
