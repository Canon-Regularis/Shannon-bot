"""Mirroring a project board that nobody will send us events about.

A board answers with every card every time it is asked, so the whole job is working out which of
them moved. Getting that wrong is not a wrong thread, it is forty threads rewritten every minute
until Discord starts refusing, which is why most of what is pinned here is what does NOT happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import COLUMN_WIDTH, TITLE_WIDTH, Repository, TrackedItem
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.github.errors import GitHubRateLimitError, GitHubUnavailableError
from shannon.services.projects import BoardItem, ProjectPoller
from shannon.services.sync.items import SyncResult, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, TicketPolicy
from shannon.services.workflow import (
    ItemWorkflow,
    WorkflowRefusedError,
    build_item_workflow,
)
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support.db import map_channel

pytestmark = pytest.mark.integration

PROJECT = 12
TICKET_CHANNEL = 4242
REPO_FULL = "canon-regularis/shannon-bot"


def card(
    item_id: int = 900,
    title: str = "Write the poller",
    column: str | None = "In Progress",
    at: str = "2026-08-20T10:00:00Z",
) -> BoardItem:
    return BoardItem(
        item_id=item_id,
        title=title,
        column=column,
        html_url=f"https://github.com/users/Canon-Regularis/projects/{PROJECT}",
        updated_at=datetime.fromisoformat(at.replace("Z", "+00:00")),
    )


class FakeBoard:
    """A project board that answers with whatever it was last set to hold."""

    def __init__(self, *items: BoardItem) -> None:
        self.items = list(items)
        self.reads: list[tuple[str, int]] = []
        self.error: Exception | None = None

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        self.reads.append((owner, project_number))
        if self.error is not None:
            raise self.error
        return list(self.items)


@pytest.fixture
async def board_channel(registered: Repository, db_session: AsyncSession) -> None:
    """Tickets have no channel fallback, so one has to be mapped before they go anywhere."""
    await map_channel(db_session, registered, ObjectType.TICKET, channel_id=TICKET_CHANNEL)


@pytest.fixture
def github_client(pr_event, issue_event) -> FakeGitHubClient:
    """GitHub holding the two items a board might wrap."""
    return FakeGitHubClient(
        pull_requests={(REPO_FULL, 7): pr_event("opened")},
        issues={(REPO_FULL, 12): issue_event("opened")},
    )


@pytest.fixture
def workflow(
    db_sessionmaker: async_sessionmaker,
    github_client: FakeGitHubClient,
    threads: FakeThreadGateway,
) -> ItemWorkflow:
    return build_item_workflow(
        db_sessionmaker,
        github_client,
        threads,
        pr_sync=build_item_sync(db_sessionmaker, threads, PullRequestPolicy()),
        issue_sync=build_item_sync(db_sessionmaker, threads, IssuePolicy()),
    )


@pytest.fixture
def poller_for(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway, workflow: ItemWorkflow
):
    def build(board: FakeBoard, *, project_number: int = PROJECT) -> ProjectPoller:
        return ProjectPoller(
            db_sessionmaker,
            board,
            build_item_sync(db_sessionmaker, threads, TicketPolicy()),
            workflow,
            project_number=project_number,
            interval=0.01,
        )

    return build


async def stored_tickets(session: AsyncSession) -> list[TrackedItem]:
    session.expire_all()
    rows = await session.scalars(
        select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.TICKET)
    )
    return list(rows.all())


class TestReadingABoard:
    async def test_a_card_gets_a_thread_in_the_ticket_channel(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        synced = await poller_for(FakeBoard(card())).run_once()

        assert synced == 1
        assert len(threads.created) == 1
        assert threads.created[0].channel_id == TICKET_CHANNEL

    async def test_the_thread_is_named_for_the_card_with_no_number_in_front(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """A card has no number, so `#0 Write the poller` would be a number invented for it."""
        await poller_for(FakeBoard(card())).run_once()

        assert threads.created[0].name == "Write the poller"

    async def test_the_block_is_the_three_lines_the_requirements_ask_for(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        await poller_for(FakeBoard(card())).run_once()

        thread = threads.created[0]
        block = thread.messages[thread.metadata_message_id]
        assert block.splitlines() == [
            "**Ticket Name:** Write the poller",
            f"**GitHub Link:** https://github.com/users/Canon-Regularis/projects/{PROJECT}",
            "**Status:** IN_REVIEW",
        ]

    async def test_the_column_becomes_the_status(
        self, board_channel: None, poller_for, db_session: AsyncSession
    ) -> None:
        await poller_for(FakeBoard(card(column="Backlog"))).run_once()

        assert (await stored_tickets(db_session))[0].status is Status.BACKLOG

    async def test_a_column_nobody_taught_us_leaves_the_status_alone(
        self, board_channel: None, poller_for, db_session: AsyncSession
    ) -> None:
        await poller_for(FakeBoard(card(column="Needs design input"))).run_once()

        assert (await stored_tickets(db_session))[0].status is Status.NOT_REVIEWED

    async def test_it_asks_about_the_owner_of_the_registered_repository(
        self, board_channel: None, poller_for
    ) -> None:
        board = FakeBoard(card())

        await poller_for(board).run_once()

        assert board.reads == [("Canon-Regularis", PROJECT)]


class TestNotDoingWorkTwice:
    """A board answers with everything every time. Most of a poll is deciding to do nothing."""

    async def test_a_card_that_has_not_moved_is_not_synced_again(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        board = FakeBoard(card())
        poller = poller_for(board)
        await poller.run_once()
        edits_after_first = len(threads.renames) + len(threads.posts)

        synced = await poller.run_once()

        assert synced == 0, "an unchanged board was mirrored a second time"
        assert len(threads.created) == 1
        assert len(threads.renames) + len(threads.posts) == edits_after_first

    async def test_a_card_the_board_lists_twice_is_mirrored_once(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """A cursor is not a snapshot. GitHub pages a list by cursor and says outright that one
        edited while it is being read can hand the same row back on two pages, which is exactly
        what a board being dragged about during a poll is. The stored state is read once for the
        whole board, so the second copy still reads as unmirrored.
        """
        board = FakeBoard(card(), card())

        mirrored = await poller_for(board).run_once()

        assert mirrored == 1, "one card listed twice was mirrored twice"
        assert len(threads.created) == 1
        assert threads.updates == [], "a thread nothing had changed was rewritten"

    async def test_a_card_that_moved_is_synced_again(
        self, board_channel: None, poller_for, db_session: AsyncSession
    ) -> None:
        board = FakeBoard(card(column="Backlog"))
        poller = poller_for(board)
        await poller.run_once()

        board.items = [card(column="Done", at="2026-08-20T11:00:00Z")]
        synced = await poller.run_once()

        assert synced == 1
        assert (await stored_tickets(db_session))[0].status is Status.DONE

    async def test_one_thread_per_card_however_often_the_board_is_read(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        board = FakeBoard(card(column="Backlog"))
        poller = poller_for(board)

        for hour, column in enumerate(["Todo", "In Progress", "Done"], start=11):
            board.items = [card(column=column, at=f"2026-08-20T{hour}:00:00Z")]
            await poller.run_once()

        assert len(threads.created) == 1


class TestWhenThereIsNothingToDo:
    async def test_no_project_configured_reads_nothing(
        self, board_channel: None, poller_for
    ) -> None:
        """Zero means none. Polling a board nobody asked for spends API calls on nothing."""
        board = FakeBoard(card())

        synced = await poller_for(board, project_number=0).run_once()

        assert synced == 0
        assert board.reads == [], "a board was read with no project configured"

    async def test_an_unregistered_guild_reads_nothing(
        self, db_sessionmaker: async_sessionmaker, poller_for
    ) -> None:
        """The process runs before anybody has run /register, which is not an error."""
        board = FakeBoard(card())

        synced = await poller_for(board).run_once()

        assert synced == 0
        assert board.reads == []


class TestTheLoop:
    async def test_a_board_it_cannot_read_does_not_end_the_loop(
        self, board_channel: None, poller_for
    ) -> None:
        """A board unreadable this minute is usually readable the next, and a poller that dies
        takes the feature with it until somebody restarts the process."""
        board = FakeBoard(card())
        board.error = RuntimeError("GitHub is having a moment")
        poller = poller_for(board)

        running = asyncio.create_task(poller.run_forever())
        # Waited for rather than slept through. A read is a database round trip and a fake call,
        # and on a loaded machine two of them do not fit inside any sleep short enough to write,
        # so a fixed wait here passes when the box is quiet and fails when it is not.
        await _until(lambda: len(board.reads) > 1)
        poller.stop()
        await asyncio.wait_for(running, timeout=5)

        assert len(board.reads) > 1, "it gave up after the first failure"

    async def test_a_spent_rate_limit_is_waited_out_rather_than_polled_through(
        self, board_channel: None, poller_for
    ) -> None:
        """GitHub answers a spent limit with the moment the window reopens, and the client
        already works that out and hands it over. Nothing read it, so the poller went back every
        interval for the whole window: a full board read a minute that cannot succeed, and for a
        secondary limit GitHub lengthens the block for every request made during one.

        The sleep below is not waiting for something to happen, which is why it is a sleep. It
        gives the failure two hundred intervals of room to appear in, against a wait of thirty
        seconds, and the point is that nothing happens in it.
        """
        board = FakeBoard(card())
        board.error = GitHubRateLimitError("GitHub rate limit reached", retry_after=30)
        poller = poller_for(board)

        running = asyncio.create_task(poller.run_forever())
        await _until(lambda: len(board.reads) >= 1)
        await asyncio.sleep(0.2)
        poller.stop()
        await asyncio.wait_for(running, timeout=5)

        assert len(board.reads) == 1, "it read the board again inside the window GitHub named"

    async def test_a_stop_does_not_wait_out_the_interval(
        self,
        board_channel: None,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        workflow: ItemWorkflow,
    ) -> None:
        """At a minute between reads, sleeping through a shutdown outlasts the grace period."""
        poller = ProjectPoller(
            db_sessionmaker,
            FakeBoard(card()),
            build_item_sync(db_sessionmaker, threads, TicketPolicy()),
            workflow,
            project_number=PROJECT,
            interval=600.0,
        )

        running = asyncio.create_task(poller.run_forever())
        await asyncio.sleep(0.1)
        poller.stop()

        await asyncio.wait_for(running, timeout=5)


def wraps(kind: ObjectType, content_id: int, column: str = "Done", item_id: int = 700) -> BoardItem:
    """A card that wraps something already mirrored from its own webhooks."""
    return BoardItem(
        item_id=item_id,
        kind=kind,
        title="Add the webhook endpoint",
        column=column,
        html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        content_id=content_id,
        updated_at=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
    )


class TestCardsWrappingSomethingAlreadyMirrored:
    """The half of "mirror board movement" that is not about drafts.

    An issue on a board already has a thread from its own webhooks. Moving its card has to move
    that thread, not open a second one, and it goes through the same path a person takes with a
    command so the GitHub label and the Discord block cannot end up disagreeing.
    """

    @pytest.fixture
    async def mirrored_pr(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> int:
        """A pull request already mirrored, and the GitHub id it was stored under."""
        snapshot = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        return snapshot.github_object_id

    async def test_a_card_moving_moves_the_status_of_the_item_it_wraps(
        self, mirrored_pr: int, poller_for, db_session: AsyncSession
    ) -> None:
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Ready for merge"))

        moved = await poller_for(board).run_once()

        assert moved == 1
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.READY_FOR_MERGE

    async def test_it_does_not_open_a_second_thread(
        self, mirrored_pr: int, poller_for, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.created)

        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        await poller_for(board).run_once()

        assert len(threads.created) == before, "the board opened a thread the item already had"

    async def test_the_github_label_follows_the_board(
        self, mirrored_pr: int, poller_for, github_client: FakeGitHubClient
    ) -> None:
        """It goes through the workflow, so the board and the labels stay in agreement."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        await poller_for(board).run_once()

        assert "IN_REVIEW" in github_client.labels[(REPO_FULL, 7)]

    async def test_a_card_already_in_the_right_column_does_nothing(
        self, mirrored_pr: int, poller_for, github_client: FakeGitHubClient
    ) -> None:
        """The stored status is compared, not a timestamp: a card and an issue keep two
        different clocks, and a card edited for any other reason must not re-assert a status."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Not reviewed"))

        moved = await poller_for(board).run_once()

        assert moved == 0
        assert github_client.label_calls == []

    async def test_a_column_nobody_taught_us_moves_nothing(
        self, mirrored_pr: int, poller_for
    ) -> None:
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Needs design input"))

        assert await poller_for(board).run_once() == 0

    async def test_a_card_wrapping_something_untracked_is_left_alone(
        self, board_channel: None, poller_for
    ) -> None:
        """A board can hold issues from repositories this guild never registered."""
        board = FakeBoard(wraps(ObjectType.ISSUE, 999_999))

        assert await poller_for(board).run_once() == 0

    async def test_a_status_the_item_cannot_hold_is_logged_and_skipped(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        poller_for,
        github_client: FakeGitHubClient,
        issue_event,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A closed issue is DONE and refuses anything else. The board may disagree with GitHub;
        it may not win, and it must not take the poll down with it."""
        closed = issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")
        await build_item_sync(db_sessionmaker, threads, IssuePolicy()).sync(closed)
        # The workflow re-reads the item from GitHub rather than trusting the stored copy, so
        # GitHub is where it has to be closed for the refusal to be the real one.
        github_client.issues[(REPO_FULL, 12)] = closed
        # Seen in Done first, which is where a closed issue belongs, so the refusal below comes
        # from a move rather than from the first look at a card nobody had recorded.
        board = FakeBoard(wraps(ObjectType.ISSUE, closed.github_object_id, column="Done"))
        poller = poller_for(board)
        await poller.run_once()

        board.items = [wraps(ObjectType.ISSUE, closed.github_object_id, column="Backlog")]
        with caplog.at_level("INFO", logger="shannon.services.projects"):
            moved = await poller.run_once()

        assert moved == 0
        assert "does not apply" in caplog.text


async def test_cancelling_the_loop_ends_it_rather_than_being_swallowed(
    board_channel: None, poller_for
) -> None:
    """The loop survives a board it cannot read, which is one instruction away from surviving
    being told to die. `except Exception` cannot catch a cancellation today, so this holds by
    itself; it is pinned because widening that clause is a one-word edit and the symptom is a
    shutdown that hangs until something kills the process."""
    reading = asyncio.Event()
    board = FakeBoard(card())

    async def block(owner: str, project_number: int):
        reading.set()
        await asyncio.sleep(60)
        return []

    board.list_board_items = block
    poller = poller_for(board)

    running = asyncio.create_task(poller.run_forever())
    # Cancelled while it is reading the board, not while it is waiting out the interval. The
    # wait sits outside the try, so a cancellation there leaves by a different door and this
    # passes without the clause it is named for ever running.
    await asyncio.wait_for(reading.wait(), timeout=5)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert running.cancelled()


class TestWhatAReviewFound:
    """Cases an adversarial pass over the poller turned up, each of which shipped broken."""

    async def test_a_card_recorded_but_never_shown_is_tried_again(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """The row is committed before the Discord call that gives it a thread.

        So a card can be stored as current with nothing to show for it, and a filter comparing
        only timestamps would never look at it again. Nothing else rescues a draft: an issue
        gets another webhook, a draft has only this poll.
        """
        threads.fail_next_create = True
        board = FakeBoard(card())
        poller = poller_for(board)

        assert await poller.run_once() == 0, "a card Discord refused was counted as mirrored"

        assert await poller.run_once() == 1, "the card was recorded and then never retried"
        assert len(threads.created) == 1

    async def test_one_bad_card_does_not_take_the_rest_with_it(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """The wrapped half already isolated each card; the draft half did not, and it runs
        first, so one card Discord refused skipped every draft after it and every wrapped card
        as well."""
        threads.fail_next_create = True
        board = FakeBoard(
            card(item_id=901, title="First"),
            card(item_id=902, title="Second"),
            card(item_id=903, title="Third"),
        )

        mirrored = await poller_for(board).run_once()

        assert mirrored == 2, "a card that failed took the ones after it down too"
        assert sorted(t.name for t in threads.created) == ["Second", "Third"]

    async def test_a_workflow_command_in_a_ticket_thread_is_refused_in_words(
        self, board_channel: None, poller_for, threads: FakeThreadGateway, workflow: ItemWorkflow
    ) -> None:
        """A draft has no repository page and no labels, so there is nothing to set. Without a
        refusal the kind lookup raised KeyError, which reaches the person who ran the command as
        "Something went wrong here" and the log as a traceback."""
        await poller_for(FakeBoard(card())).run_once()
        thread_id = threads.created[0].thread_id

        with pytest.raises(WorkflowRefusedError, match="no GitHub labels to set"):
            await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        with pytest.raises(WorkflowRefusedError, match="Move its card on the board"):
            await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)


class TestTheBoardDoesNotOverruleACommand:
    """The board moves things; it does not get to win an argument it was not part of.

    Before this, `_move_tracked` compared the card's column against the item's stored status and
    acted whenever they differed. A reviewer running /set_ready_for_merge on a pull request whose
    card still sat in `In Progress` had the decision reverted by the next poll, silently, inside
    the interval, because a standing disagreement and a fresh move looked identical from here.
    """

    @pytest.fixture
    async def mirrored_pr(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> int:
        snapshot = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        return snapshot.github_object_id

    async def test_a_card_that_has_not_moved_leaves_a_command_alone(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        poller = poller_for(board)
        await poller.run_once()

        thread_id = threads.created[0].thread_id
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)

        assert await poller.run_once() == 0, "the board overruled a command"
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.READY_FOR_MERGE

    async def test_a_card_that_does_move_is_still_followed(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The other half. Ignoring a genuine move would be the same defect the other way up."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        poller = poller_for(board)
        await poller.run_once()
        await workflow.set_status(
            thread_id=threads.created[0].thread_id, status=Status.READY_FOR_MERGE
        )

        board.items = [wraps(ObjectType.PR, mirrored_pr, column="Backlog")]

        assert await poller.run_once() == 1
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.BACKLOG

    async def test_the_first_look_fills_in_an_item_nobody_has_decided_about(
        self, mirrored_pr: int, poller_for, db_session: AsyncSession
    ) -> None:
        """A freshly mirrored pull request is NOT_REVIEWED, which is nobody's opinion."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))

        assert await poller_for(board).run_once() == 1
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.IN_REVIEW

    async def test_the_first_look_does_not_overwrite_a_decision(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A board added after the fact must not undo what was decided before it existed."""
        await workflow.set_status(
            thread_id=threads.created[0].thread_id, status=Status.READY_FOR_MERGE
        )
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Backlog"))

        with caplog.at_level("INFO", logger="shannon.services.projects"):
            assert await poller_for(board).run_once() == 0

        assert "first look at its card" in caplog.text
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.READY_FOR_MERGE

    async def test_a_move_the_item_cannot_take_is_not_asked_about_again(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        poller_for,
        github_client: FakeGitHubClient,
        issue_event,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A refusal is still a move. Remembering only what worked would repeat the same
        complaint on every poll for the life of the process."""
        closed = issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")
        await build_item_sync(db_sessionmaker, threads, IssuePolicy()).sync(closed)
        github_client.issues[(REPO_FULL, 12)] = closed
        board = FakeBoard(wraps(ObjectType.ISSUE, closed.github_object_id, column="Done"))
        poller = poller_for(board)
        await poller.run_once()

        # The move that gets refused. Recorded anyway, so the next poll sees no move at all.
        board.items = [wraps(ObjectType.ISSUE, closed.github_object_id, column="Backlog")]
        await poller.run_once()

        with caplog.at_level("INFO", logger="shannon.services.projects"):
            await poller.run_once()

        assert "does not apply" not in caplog.text, "it complained about the same card twice"


class TestWhenSomethingElseGoesWrongMidPoll:
    """A failure that is not the board disagreeing must not be reported as if it were."""

    @pytest.fixture
    async def mirrored_pr(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> int:
        snapshot = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        return snapshot.github_object_id

    async def test_a_github_outage_is_not_logged_as_a_column_mismatch(
        self,
        mirrored_pr: int,
        poller_for,
        github_client: FakeGitHubClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Every GitHub and Discord failure is a ShannonError, so catching that alone reported an
        outage as forty cards with unsuitable columns."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        github_client.error = GitHubUnavailableError("GitHub is down")

        with caplog.at_level("INFO", logger="shannon.services.projects"):
            moved = await poller_for(board).run_once()

        assert moved == 0
        assert "could not move" in caplog.text
        assert "does not apply" not in caplog.text, "an outage was reported as a bad column"

    async def test_a_card_wrapping_nothing_identifiable_is_passed_over(
        self, board_channel: None, poller_for
    ) -> None:
        """`content_id` is the only handle on what a card wraps. Without it there is no row to
        find, and a card that reached here without one is one the parser could not read."""
        board = FakeBoard(
            BoardItem(
                item_id=800,
                kind=ObjectType.ISSUE,
                title="A card with no content id",
                column="Done",
                html_url="https://github.com/x",
                content_id=None,
            )
        )

        assert await poller_for(board).run_once() == 0


class TestASecondReviewFound:
    """A move that failed for a bad reason must not be written off as seen."""

    @pytest.fixture
    async def mirrored_pr(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> int:
        snapshot = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        return snapshot.github_object_id

    async def test_a_move_that_failed_is_tried_again_when_github_recovers(
        self,
        mirrored_pr: int,
        poller_for,
        github_client: FakeGitHubClient,
        db_session: AsyncSession,
    ) -> None:
        """The column used to be written before the move was attempted, so a rate limit or a 500
        left the card recorded in its new column with the old status, and no later poll ever
        looked at it again. Nothing else rederives a status from a board."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Ready for merge"))
        poller = poller_for(board)
        github_client.error = GitHubUnavailableError("GitHub is down")

        assert await poller.run_once() == 0

        github_client.error = None
        assert await poller.run_once() == 1, "the failed move was written off as seen"
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.READY_FOR_MERGE

    async def test_a_card_first_seen_with_no_status_is_still_remembered(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """A card added to a board carries no Status until somebody picks one, so this is what
        most cards look like on the poll that first sees them.

        Nothing moved and nothing was ever seen look identical at the top of the loop, so the
        card was passed over without its column being written down. That leaves the column null,
        null means never seen, and the first-look guard is still armed when the Status is finally
        set: the move that sets it is read as a first look at the card and dropped, and no later
        poll revisits it because the column then matches.
        """
        await workflow.set_status(
            thread_id=threads.created[0].thread_id, status=Status.READY_FOR_MERGE
        )
        poller = poller_for(FakeBoard(wraps(ObjectType.PR, mirrored_pr, column=None)))
        assert await poller.run_once() == 0

        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Backlog"))

        assert await poller_for(board).run_once() == 1, "the first real move was written off"
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.BACKLOG

    async def test_a_card_that_reads_as_having_no_column_keeps_the_one_it_had(
        self, mirrored_pr: int, poller_for, db_session: AsyncSession
    ) -> None:
        """One card with no column and a board whose Status field cannot be read look the same.

        The second is the one that cannot be survived: every card on the board reads blank at
        once, and believing it means writing the blank over every remembered column, so the poll
        after the field comes back reads the whole board as having moved and drives all of it
        through the status commands. Whatever anybody had set by hand goes with it.

        Nothing is lost by keeping the old column instead. A card with no column carries no
        status to move to, so it is passed over either way, and the memory that is kept is one
        the card is no longer in, which the next real move still differs from.
        """
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        poller = poller_for(board)
        await poller.run_once()

        board.items = [wraps(ObjectType.PR, mirrored_pr, column=None)]
        await poller.run_once()

        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.project_column == "In Progress", "the blank was written over the memory"

    async def test_a_board_that_goes_blank_does_not_restate_itself_when_it_comes_back(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The whole failure, end to end: read the board, lose the Status field, get it back.

        A board's Status field is matched by name, so renaming it is enough, and the ids are
        looked up per board. In between, somebody decides an item is not what the board says and
        sets it by hand. That decision has to survive the field coming back.
        """
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        poller = poller_for(board)
        await poller.run_once()

        # The Status field renamed, which every card answers by carrying no column at all.
        board.items = [wraps(ObjectType.PR, mirrored_pr, column=None)]
        await poller.run_once()

        # Somebody looks at the item and decides otherwise while the board cannot be read.
        await workflow.set_status(
            thread_id=threads.created[0].thread_id, status=Status.READY_FOR_MERGE
        )

        # The field comes back, unchanged, saying what it said before.
        board.items = [wraps(ObjectType.PR, mirrored_pr, column="In Progress")]

        assert await poller.run_once() == 0, "a board that had not moved restated itself"
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.READY_FOR_MERGE, "the board overwrote a decision"

    async def test_a_cleared_column_does_not_re_arm_the_first_look_guard(
        self,
        mirrored_pr: int,
        poller_for,
        workflow: ItemWorkflow,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The whole point of the distinction: after a clear, the next real move still lands."""
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="In Progress"))
        poller = poller_for(board)
        await poller.run_once()
        await workflow.set_status(
            thread_id=threads.created[0].thread_id, status=Status.READY_FOR_MERGE
        )

        board.items = [wraps(ObjectType.PR, mirrored_pr, column=None)]
        await poller.run_once()
        board.items = [wraps(ObjectType.PR, mirrored_pr, column="Backlog")]

        assert await poller.run_once() == 1, "the move after a cleared column was dropped"
        db_session.expire_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item.status is Status.BACKLOG


class TestABoardNobodyFinishedSettingUp:
    """Tickets have no channel fallback, so a board configured before `/set_channel` mirrors
    nothing at all. What it must not do is say so once per card, once a minute, for ever."""

    async def test_the_missing_channel_is_said_once_rather_than_once_per_card(
        self, registered: Repository, poller_for, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing about any one card decided this: the sync refuses on the repository or on the
        channel, so every card after the first is refused identically, and each one opens a
        session and runs two queries to learn the same thing. On a board of any size that is the
        whole log, every interval, until somebody notices the one line that matters.
        """
        board = FakeBoard(*(card(item_id=900 + n, title=f"Card {n}") for n in range(6)))

        with caplog.at_level("WARNING"):
            assert await poller_for(board).run_once() == 0

        per_card = [r for r in caplog.records if "has no channel mapped" in r.getMessage()]
        assert len(per_card) == 1, f"six cards were each asked and refused: {len(per_card)} times"
        assert any("run /set_channel" in r.getMessage() for r in caplog.records), (
            "it stopped without saying what to do about it"
        )


class TestOneCardTakingTheWholeBoardWithIt:
    """A poll reads the whole board, and used to act on all of it or on none of it.

    Every failure here has the same shape. A card that raises where nobody wrote a branch ends
    `run_once` half way, so the cards behind it and the wrapped half after them are skipped, and
    since nothing was recorded the next poll reads the same board and stops at the same card. It
    does not right itself and it does not degrade: the feature is off, for the life of the
    process, and the only sign of it is one traceback a minute.
    """

    @pytest.fixture
    async def mirrored(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
        issue_event,
    ) -> tuple[int, int]:
        """A pull request and an issue, both already mirrored from their own webhooks."""
        pull = pr_event("opened")
        issue = issue_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(pull)
        await build_item_sync(db_sessionmaker, threads, IssuePolicy()).sync(issue)
        return pull.github_object_id, issue.github_object_id

    async def test_a_draft_wider_than_its_row_is_cut_rather_than_ending_the_poll(
        self, board_channel: None, poller_for, db_session: AsyncSession
    ) -> None:
        """GitHub caps an issue title at 256 characters, comfortably inside the row. A draft
        card's Title is a free text field with no cap at all, so a card anybody with write
        access can make raised out of the flush, past the per-card handling, and killed the
        board mirror until somebody happened to edit that one card.
        """
        board = FakeBoard(
            card(item_id=901, title="A" * (TITLE_WIDTH + 200)),
            card(item_id=902, title="An ordinary card"),
        )

        mirrored = await poller_for(board).run_once()

        assert mirrored == 2, "one wide card took the cards behind it with it"
        assert {len(row.title) for row in await stored_tickets(db_session)} == {
            TITLE_WIDTH,
            len("An ordinary card"),
        }

    async def test_a_status_column_wider_than_its_row_is_cut_and_the_card_behind_it_still_moves(
        self, mirrored: tuple[int, int], poller_for, db_session: AsyncSession
    ) -> None:
        """The board's Status is matched by field name and never by field type, so what reaches
        here can be free text somebody pasted in. The card behind it is the point: this half of
        the poll had no per-card handling at all, so the first raise ended the lot.
        """
        pull, issue = mirrored
        wide = "Backlog " + "z" * 400
        board = FakeBoard(
            wraps(ObjectType.PR, pull, column=wide, item_id=701),
            wraps(ObjectType.ISSUE, issue, column="In Progress", item_id=702),
        )

        assert await poller_for(board).run_once() == 1, "a wide column took the next card with it"

        db_session.expire_all()
        rows = (await db_session.scalars(select(TrackedItem).order_by(TrackedItem.id))).all()
        by_type = {row.github_object_type: row for row in rows}
        assert by_type[ObjectType.PR].project_column == wide[:COLUMN_WIDTH]
        assert by_type[ObjectType.ISSUE].status is Status.IN_REVIEW

    async def test_a_card_the_sync_refused_is_not_counted_as_mirrored(
        self, registered: Repository, poller_for, threads: FakeThreadGateway
    ) -> None:
        """Tickets have no channel fallback, so a board configured before anybody ran
        `/set_channel` mirrors nothing at all. Counting the attempt had every poll report a
        board being mirrored, for ever, directly under the warning saying the opposite.
        """
        board = FakeBoard(card(item_id=901), card(item_id=902))

        assert await poller_for(board).run_once() == 0
        assert threads.created == []

    async def test_a_draft_the_sync_did_not_expect_to_fail_on_is_still_only_one_card(
        self,
        board_channel: None,
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        workflow: ItemWorkflow,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Not every failure is a ShannonError. The one that got here first was a card too wide
        for its column, which arrives as a DBAPIError out of the flush.
        """
        sync = ExplodingSync()
        board = FakeBoard(card(item_id=901), card(item_id=902))
        poller = ProjectPoller(
            db_sessionmaker, board, sync, workflow, project_number=PROJECT, interval=0.01
        )

        with caplog.at_level("ERROR", logger="shannon.services.projects"):
            assert await poller.run_once() == 0

        assert sync.calls == 2, "the first surprise took the card behind it with it"
        assert "could not mirror the card" in caplog.text

    async def test_a_move_the_workflow_did_not_expect_to_fail_on_is_still_only_one_card(
        self,
        mirrored: tuple[int, int],
        db_sessionmaker: async_sessionmaker,
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pull, issue = mirrored
        moves = ExplodingWorkflow()
        board = FakeBoard(
            wraps(ObjectType.PR, pull, column="In Progress", item_id=701),
            wraps(ObjectType.ISSUE, issue, column="In Progress", item_id=702),
        )
        poller = ProjectPoller(
            db_sessionmaker,
            board,
            build_item_sync(db_sessionmaker, threads, TicketPolicy()),
            moves,
            project_number=PROJECT,
            interval=0.01,
        )

        with caplog.at_level("ERROR", logger="shannon.services.projects"):
            assert await poller.run_once() == 0

        assert moves.calls == 2, "the first surprise took the card behind it with it"
        assert "could not move the card" in caplog.text


class TestProgressRecordedForAStepThatFailed:
    """The other shape a permanently wrong card takes, and the harder one to see.

    Nothing raises out of the poll and nothing is logged as an error. A run gets half way, writes
    down the half it did, and the next poll reads that half as the whole and skips the card. The
    board and Discord then disagree for ever about an item nobody will touch again.
    """

    @pytest.fixture
    async def mirrored_pr(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker,
        pr_event,
    ) -> int:
        snapshot = pr_event("opened")
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        return snapshot.github_object_id

    async def test_a_move_to_done_whose_lock_was_refused_locks_on_the_next_poll(
        self, mirrored_pr: int, poller_for, threads: FakeThreadGateway
    ) -> None:
        """Locking is a separate permission on Discord's side and the last step of finishing an
        item, so it is the likeliest of them to fail on its own. The status was already written
        by then, and matching statuses used to end the card: a finished pull request kept an
        open thread for ever, and no poll ever looked at it again.
        """
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Ready for merge"))
        poller = poller_for(board)
        await poller.run_once()

        board.items = [wraps(ObjectType.PR, mirrored_pr, column="Done")]
        threads.fail_next_lock = True
        assert await poller.run_once() == 0

        thread_id = threads.created[0].thread_id
        assert threads.threads[thread_id].locked is False

        assert await poller.run_once() == 1, "the lock nobody got round to was written off as done"
        assert threads.threads[thread_id].locked is True

    async def test_a_wrapped_card_listed_twice_is_only_acted_on_once(
        self, mirrored_pr: int, poller_for, github_client
    ) -> None:
        """The draft half refused a repeat and the wrapped half did not, in the same function.

        The reason given for the draft guard is a property of the read rather than of drafts: a
        board is paged by cursor, and GitHub documents that a list edited while it is being paged
        can hand the same row back on two pages, which is what a board being dragged is. The
        state both halves judge against is read once for the whole board and never written to, so
        the second copy is judged against the state before the first was acted on.

        For a wrapped card that cost a second GitHub read of the item on every poll that saw it,
        against the same rate limit the board read and every command draw on, and made the pass
        report two moves where one had happened.
        """
        board = FakeBoard(
            wraps(ObjectType.PR, mirrored_pr, column="In Review"),
            wraps(ObjectType.PR, mirrored_pr, column="In Review"),
        )
        reads = len(github_client.pull_request_calls)

        moved = await poller_for(board).run_once()

        assert moved == 1, "one card moving was counted twice"
        assert len(github_client.pull_request_calls) - reads == 1, "it read the item twice"

    async def test_a_lock_no_permission_will_ever_grant_is_not_asked_for_every_minute(
        self, mirrored_pr: int, poller_for, threads: FakeThreadGateway, github_client
    ) -> None:
        """The retry above is for a bad moment. A missing permission is not one.

        Nothing else advances the column, so a card whose lock is refused comes round on every
        poll for as long as the refusal lasts. When the refusal is a permission, that is for
        ever, and every card the team ever finishes joins the set and never leaves it: a GitHub
        read and a Discord call each, once a minute, growing with throughput.

        The move itself did land, so it is written off as carried through, which is what the
        column records, and the reason is said once rather than once a minute.
        """
        board = FakeBoard(wraps(ObjectType.PR, mirrored_pr, column="Ready for merge"))
        poller = poller_for(board)
        await poller.run_once()

        board.items = [wraps(ObjectType.PR, mirrored_pr, column="Done")]
        threads.refuses_every_lock = True
        assert await poller.run_once() == 1, "the move it did carry out was reported as none"

        reads = len(github_client.pull_request_calls)
        assert await poller.run_once() == 0
        assert await poller.run_once() == 0
        assert github_client.pull_request_calls[reads:] == [], "it asked GitHub every poll"

    async def test_a_draft_whose_thread_edit_was_refused_is_mirrored_again(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """The row is committed before the Discord call that shows it, deliberately, and a
        delivery that fails after it is retried by the worker. A card has no worker: it comes
        back only when GitHub's timestamp beats the stored one, and the failed sync had just
        made those equal.
        """
        board = FakeBoard(card())
        poller = poller_for(board)
        await poller.run_once()
        thread_id = threads.created[0].thread_id

        board.items = [card(title="Write the poller, properly", at="2026-08-21T10:00:00Z")]
        threads.fail_next_update = True
        assert await poller.run_once() == 0

        assert await poller.run_once() == 1, "a refused edit left the thread wrong for ever"
        assert threads.threads[thread_id].name == "Write the poller, properly"

    async def test_a_draft_whose_very_first_message_was_refused_is_mirrored_again(
        self, board_channel: None, poller_for, threads: FakeThreadGateway
    ) -> None:
        """The same repair, on the mirror that has nothing stored to put back.

        Opening a thread and writing the first message in it are two Discord calls and two
        permissions, so a server that grants Create Public Threads and not Send Messages in
        Threads refuses the second every time. The thread is real by then and the sync attaches
        it on purpose, so the row ends up holding both the card's timestamp and a thread id.

        Putting the mark back used to be skipped whenever nothing had been stored before, on the
        grounds that a card with no timestamp or no thread is offered again anyway. This row has
        both, so neither escape applied: the card compared its own timestamp with itself for
        ever, and Discord kept an empty thread named after it with no block in it.
        """
        board = FakeBoard(card())
        poller = poller_for(board)
        threads.fail_next_first_message = True

        assert await poller.run_once() == 0
        empty = threads.created[0]
        assert empty.metadata_message_id is None, "nothing was refused, so this proves nothing"

        assert await poller.run_once() == 1, "the card was never offered again"
        assert threads.threads[empty.thread_id].metadata_message_id is not None
        assert len(threads.created) == 1, "it opened a second thread beside the empty one"


class ExplodingSync:
    """A sync that fails the way nobody wrote a branch for, counting how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def sync(self, snapshot) -> SyncResult:
        self.calls += 1
        raise RuntimeError("something nobody wrote a branch for")


class ExplodingWorkflow:
    """The same, for the half of the poll that moves an item somebody else already mirrored."""

    def __init__(self) -> None:
        self.calls = 0

    async def set_status(self, *, thread_id: int, status: Status) -> object:
        self.calls += 1
        raise RuntimeError("something nobody wrote a branch for")


async def _until(condition, timeout: float = 10.0) -> None:
    """Wait for something the poller does on its own schedule, rather than guessing at a sleep."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
