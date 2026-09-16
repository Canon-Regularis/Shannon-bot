"""A label going on or coming off says so in the thread.

The metadata block already lists every label and is rewritten on every delivery, so the state was
never wrong. What was missing is that a Discord edit is silent: it posts no message, notifies
nobody and does not bump the thread, so tagging an item looked from the channel exactly like
nothing happening. Reported as issue #62 after watching it happen in a real server.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote, Repository
from shannon.discord_bot import formatting
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_label_change
from shannon.github.webhooks.issues import parse_issue_event
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.label_lines import LabelLine
from shannon.services.sync.policies import IssuePolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration


# Read off the renderer rather than restated. "Tag " stopped being the one lead when the line
# split into three groups, and pasting the marks here instead would be a filter that quietly
# matches nothing the moment one of them gains or loses a variation selector: every assertion
# below would then pass against an empty list.
MARKS = (
    *formatting._PRIORITY_MARKS.values(),
    formatting._PRIORITY_GONE,
    formatting._STATUS_MARK,
    formatting._TAG_MARK,
)


def lines(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if body.startswith(MARKS)]


def labelled(action: str, name: str) -> dict:
    """An issue event carrying the label that moved, the way GitHub sends one.

    The payload helper builds only the inner issue, so the top-level `label` is added here. That
    is the whole of what separates these two actions from every other: one delivery, one label,
    named where nothing else names it.

    A test about the claim wants a name the opening block never showed, because a name it did
    show is suppressed on purpose and the line would be missing for that reason instead of the
    one under test. `bug` is the helper's default and is exactly the wrong choice.
    """
    payload = payloads.issue_event(action, labels=[{"name": name}])
    payload["label"] = {"name": name, "color": "d73a4a"}
    return payload


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

    assert lines(threads) == ["🔴 **Priority set:** `high priority`"]


async def test_a_label_coming_off_says_so(registered: Repository, db_engine: AsyncEngine) -> None:
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("unlabeled", "wontfix"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["🏷️ Tag `wontfix` removed."]


async def test_a_label_the_opening_block_already_showed_says_nothing(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """`bug` is on the issue when it is opened, so the block that went up a moment ago lists it.

    GitHub sends the `labeled` delivery for it beside the `opened` one rather than folding the
    two together, which is why anything ever said it at all. Issue #81.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("labeled", "bug"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == []


async def test_a_label_that_came_off_and_went_back_on_is_said_both_times(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """Taking `bug` off is news even though the block showed it, and once a reader has been told
    it is gone, putting it back is news again.

    What keeps those apart is that the set follows the LINES rather than the labels: a line
    saying a tag came off takes the name out of it.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("unlabeled", "bug"), delivery="tag-1")
        await container.worker.run_once()
        await post(client, "issues", labelled("labeled", "bug"), delivery="tag-2")
        await container.worker.run_once()

    assert lines(threads) == ["🏷️ Tag `bug` removed.", "🏷️ Tag `bug` added."]


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
    announcer = LabelLine(
        db_sessionmaker,
        threads,
        render=format_label_change,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
    )
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)
    payload = labelled("labeled", "needs design")

    await handle("labeled", payload, 900_002)
    await handle("labeled", payload, 900_002)

    assert lines(threads) == ["🏷️ Tag `needs design` added."]
    held = await db_session.scalar(select(func.count()).select_from(MirroredNote))
    assert held == 1, "the claim that makes it say it once was not taken"


async def test_a_refused_post_gives_the_claim_back_so_the_retry_says_it(
    registered: Repository, db_sessionmaker: async_sessionmaker
) -> None:
    """A claim taken and not given back is worse than saying nothing: the retry reads it as
    already said and the line is lost for good, with the delivery reported handled."""
    threads = _RefusesTheFirstPost()
    announcer = LabelLine(
        db_sessionmaker,
        threads,
        render=format_label_change,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
    )
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)
    payload = labelled("labeled", "needs design")

    with pytest.raises(DiscordGatewayError):
        await handle("labeled", payload, 900_002)
    assert lines(threads) == [], "it says nothing when the post was refused"

    await handle("labeled", payload, 900_002)

    assert lines(threads) == ["🏷️ Tag `needs design` added."], "the claim was never given back"


async def test_a_claim_that_cannot_be_given_back_is_said_loudly(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both halves failing at once is rare and unrecoverable, so the one thing owed is a line
    saying which row to remove to have it said again."""
    threads = _RefusesTheFirstPost()
    announcer = LabelLine(
        _FailsToGiveItBack(db_sessionmaker),
        threads,
        render=format_label_change,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
    )
    handle = build_item_handler(
        build_item_sync(db_sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=announcer,
    )
    await handle("opened", payloads.issue_event("opened"), 900_001)

    with caplog.at_level(logging.ERROR), pytest.raises(DiscordGatewayError):
        await handle("labeled", labelled("labeled", "needs design"), 900_002)

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
        # Two: the announcer reads what the block already showed before it claims anything, and
        # the read has to work or the line is never reached at all.
        self._left = 2

    def __call__(self):
        if self._left:
            self._left -= 1
            return self._sessionmaker()
        raise RuntimeError("the database went away")


async def test_an_event_that_moves_no_label_says_nothing(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """An `edited` delivery carries the whole label list and names no move, so there is nothing
    to announce from it. Only `labeled` and `unlabeled` say which one went on or came off, and
    GitHub sends one of those per label rather than folding them into the delivery that opened
    the item."""
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
    assert lines(threads) == ["🏷️ Tag `needs design` added."]


async def test_a_status_label_is_not_announced_as_an_ordinary_tag(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """The five statuses live as labels on the repository and this bot writes them itself, so a
    `/set_done` and somebody tagging an issue `bug` arrive down the same webhook. Saying the same
    sentence about both buried the one that matters under the one that does not.

    Through the whole stack rather than against the renderer, because the classification happens
    where the delivery is read and the renderer test cannot tell whether that is wired.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("labeled", "IN_REVIEW"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["📋 **Status set:** `IN_REVIEW`"]


async def test_a_priority_the_repository_spells_its_own_way_is_still_read_as_one(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """Priority has been read off whatever spelling a repository already uses since MVP 2, so
    `urgent` is a priority coming off and not an ordinary tag. Named the way the repository
    wrote it rather than translated into ours, because the label on GitHub is what somebody
    looking for it will search for."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issues", labelled("unlabeled", "urgent"), delivery="tag-1")
        await container.worker.run_once()

    assert lines(threads) == ["⚪ **Priority cleared:** `urgent`"]
