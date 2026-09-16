"""An item closing, merging or reopening says so in the thread.

The loudest case of the same silence the tag line answers. Closing an issue rewrites the metadata
block and locks the thread, and Discord announces neither: an edit posts no message, notifies
nobody and does not bump the thread, and a lock is not an event at all. So an item could close,
shut the discussion under it, and leave nothing whatever in the channel. The only text anybody saw
was the `/set_done` reply, and every command reply here is ephemeral, so nobody but the person who
ran it ever read one. Reported as issue #73.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote, Repository, TrackedItem
from shannon.discord_bot.errors import DiscordPermissionError
from shannon.discord_bot.formatting import format_state_change
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from shannon.services.sync.state_lines import StateLine
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration

CLOSED = {"state": "closed", "closed_at": "2026-08-11T12:00:00Z"}
MERGED = {"state": "closed", "merged": True, "merged_at": "2026-08-10T13:00:00Z"}
SHUT = (
    "### 🔒 Closed\n-# This thread is locked and archived. "
    "Reopen the item on GitHub to reopen it here."
)
MERGED_SHUT = "### 🟣 Merged\n-# This thread is locked and archived."
REOPENED = "### 🔓 Reopened"


def markers(threads: FakeThreadGateway) -> list[str]:
    """Only the headers, so an assertion cannot be satisfied by a tag line landing beside one."""
    return [body for _, body in threads.posts if body.startswith("###")]


def issue_handler(sessionmaker: async_sessionmaker, threads: FakeThreadGateway):
    return build_item_handler(
        build_item_sync(sessionmaker, threads, IssuePolicy()),
        parse_issue_event,
        announce=StateLine(
            sessionmaker,
            threads,
            render=format_state_change,
            shut_again=KeepsThreadsShut(sessionmaker, threads),
        ),
    )


def pull_request_handler(sessionmaker: async_sessionmaker, threads: FakeThreadGateway):
    return build_item_handler(
        build_item_sync(sessionmaker, threads, PullRequestPolicy()),
        parse_pull_request_event,
        announce=StateLine(
            sessionmaker,
            threads,
            render=format_state_change,
            shut_again=KeepsThreadsShut(sessionmaker, threads),
        ),
    )


async def move_the_item_on(sessionmaker: async_sessionmaker, *, state: str, at: datetime) -> None:
    """What a later delivery leaves on the row, written directly.

    Directly rather than by putting another delivery through, because a sync running inside
    another sync deadlocks against the per item lock, which is how that was learned once already.
    The stamp is what the staleness guard reads and the state is what this announcer reads, so
    both are set or the row says half of each.
    """
    async with sessionmaker() as session, session.begin():
        item = await session.scalar(select(TrackedItem))
        assert item is not None
        item.github_state = state
        item.github_updated_at = at


class _RefusesToGiveTheThreadBack(FakeThreadGateway):
    """A server that lets the bot shut a thread and will not let it open one again.

    Locking and unlocking are one permission on Discord's side, so this shape is contrived. The
    refusal is not: a bot removed from the server answers the same way, and the unlock is the
    first Discord call a reopen makes.
    """

    async def set_shut(self, *, thread_id: int, shut: bool) -> None:
        if not shut:
            self.shut_calls.append((thread_id, shut))
            raise DiscordPermissionError("Discord will not let the bot reopen the thread")
        await super().set_shut(thread_id=thread_id, shut=shut)


class TestWhatEachMoveSays:
    async def test_closing_an_issue_says_so_and_says_the_thread_is_shut(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)

        await handle("opened", payloads.issue_event("opened"), 900_001)
        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)

        assert markers(threads) == [SHUT]
        thread = threads.threads[threads.created[0].thread_id]
        assert thread.locked is True
        # The header is posted after the sync shut the thread, and posting reopens an archived
        # one. Without putting it back, every closed item ends up announced as closed in a thread
        # that is sitting open in the channel.
        assert thread.archived is True, "the header it posted left the thread open"

    async def test_reopening_an_issue_says_the_thread_is_back(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)

        await handle("opened", payloads.issue_event("opened"), 900_001)
        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)
        await handle(
            "reopened",
            payloads.issue_event("reopened", updated_at="2026-08-11T13:00:00Z"),
            900_003,
        )

        assert markers(threads)[-1] == f"{REOPENED}\n-# This thread is open again."
        assert threads.threads[threads.created[0].thread_id].locked is False

    async def test_a_pull_request_closed_without_merging_is_shut_too(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """An abandoned pull request is exactly the thread worth getting out of the way, and it
        is the one that can genuinely be reopened on GitHub, so it is told how.
        """
        threads = FakeThreadGateway()
        handle = pull_request_handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await handle("closed", payloads.pull_request_event("closed", **CLOSED), 900_002)

        assert markers(threads) == [SHUT]
        assert threads.threads[threads.created[0].thread_id].archived is True

    async def test_a_merged_pull_request_is_told_apart_from_an_abandoned_one(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """GitHub has no `merged` action: a merge arrives as a close with a flag beside it, and
        work finished reads differently from work dropped.
        """
        threads = FakeThreadGateway()
        handle = pull_request_handler(db_sessionmaker, threads)

        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await handle("closed", payloads.pull_request_event("closed", **MERGED), 900_002)

        assert markers(threads) == [MERGED_SHUT]
        assert threads.threads[threads.created[0].thread_id].archived is True

    async def test_a_thread_discord_would_not_shut_says_which_permission_is_missing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """The whole point of carrying the refusal instead of raising on it.

        This delivery used to be dropped: a missing permission is permanent, so the worker gave
        up on the first attempt and the header never posted. What a reader saw was a thread that
        went quiet, with the reason in a log on a server they cannot get at. Now the item still
        says it closed and the thread says why it is still open.
        """
        threads = FakeThreadGateway()
        threads.refuses_every_shut = True
        handle = issue_handler(db_sessionmaker, threads)

        await handle("opened", payloads.issue_event("opened"), 900_001)
        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)

        assert markers(threads) == [
            "### 🔒 Closed\n-# This thread could not be closed: the bot needs Manage Threads."
        ]
        assert threads.threads[threads.created[0].thread_id].archived is False

    async def test_an_action_that_moves_no_state_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """An `edited` delivery carries the item state too. One that reads closed did not just
        close: it was closed already, and something else about it changed.
        """
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)

        await handle("opened", payloads.issue_event("opened"), 900_001)
        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)
        await handle(
            "edited",
            payloads.issue_event("edited", updated_at="2026-08-11T13:00:00Z", **CLOSED),
            900_003,
        )

        assert len(markers(threads)) == 1, "an edit of a closed issue announced it closing again"


class TestSayingItOnce:
    async def test_the_same_delivery_handled_twice_says_it_once(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
    ) -> None:
        """The queue is at-least-once by design: a delivery whose status could not be written
        comes back when its lease runs out and is handled again from the top. A thread reading two
        identical headers invites the reader to look for two closes.
        """
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)
        await handle("opened", payloads.issue_event("opened"), 900_001)
        closing = payloads.issue_event("closed", **CLOSED)

        await handle("closed", closing, 900_002)
        await handle("closed", closing, 900_002)

        assert markers(threads) == [SHUT]
        held = await db_session.scalar(
            select(func.count())
            .select_from(MirroredNote)
            .where(MirroredNote.note_key == "state:900002")
        )
        assert held == 1, "the claim that makes it say it once was not taken"


class TestAnItemThatHasMovedOnSince:
    """The gate is the row rather than the outcome of the sync, and these tell the two apart."""

    async def test_a_close_overtaken_by_a_reopen_says_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """Otherwise a close announces itself for an item that is open, under a block that says
        Open, and a reader has no way to tell which of the two to believe.
        """
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)
        await handle("opened", payloads.issue_event("opened"), 900_001)
        await move_the_item_on(
            db_sessionmaker, state="open", at=datetime(2026, 8, 11, 14, 0, tzinfo=UTC)
        )

        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)

        assert markers(threads) == []

    async def test_a_close_overtaken_by_something_that_still_says_closed_is_still_said(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """The case a gate on the outcome of the sync would lose for good. This delivery is
        refused as superseded, whatever overtook it was not a state move so it announced nothing,
        and a closed item sends no further events, so nothing would ever come back for it.
        """
        threads = FakeThreadGateway()
        handle = issue_handler(db_sessionmaker, threads)
        await handle("opened", payloads.issue_event("opened"), 900_001)
        await move_the_item_on(
            db_sessionmaker, state="closed", at=datetime(2026, 8, 11, 14, 0, tzinfo=UTC)
        )

        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)

        assert markers(threads) == [SHUT]

    async def test_a_reopen_whose_unlock_was_refused_does_not_promise_the_thread_is_open(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        """A refused unlock is logged and stepped over rather than failing the delivery, on
        purpose, so a reopened item really does reach the announcement in a thread that is still
        shut. Reading the row rather than restating which kinds lock is what keeps this honest.
        """
        threads = _RefusesToGiveTheThreadBack()
        handle = issue_handler(db_sessionmaker, threads)
        await handle("opened", payloads.issue_event("opened"), 900_001)
        await handle("closed", payloads.issue_event("closed", **CLOSED), 900_002)

        await handle(
            "reopened",
            payloads.issue_event("reopened", updated_at="2026-08-11T13:00:00Z"),
            900_003,
        )

        assert markers(threads)[-1] == REOPENED, (
            "it told people a thread they still cannot post in is open again"
        )
        assert threads.threads[threads.created[0].thread_id].locked is True


async def test_the_block_is_still_written_on_the_same_delivery(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """The marker is an announcement, not a replacement. Whoever reads the thread a week later
    scrolls to the block, and it has to say the item is closed.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await post(client, "issues", payloads.issue_event("opened"), delivery="issue-1")
        await container.worker.run_once()
        await post(client, "issues", payloads.issue_event("closed", **CLOSED), delivery="shut-1")
        await container.worker.run_once()

    thread_id = threads.created[0].thread_id
    assert "**State:** Closed" in threads.metadata_of(thread_id), "the block did not keep up"
    assert markers(threads) == [SHUT]
