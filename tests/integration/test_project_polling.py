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

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.github.errors import GitHubUnavailableError
from shannon.services.projects import BoardItem, ProjectPoller
from shannon.services.sync.items import build_item_sync
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
        await asyncio.sleep(0.1)
        poller.stop()
        await asyncio.wait_for(running, timeout=5)

        assert len(board.reads) > 1, "it gave up after the first failure"

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
    poller = poller_for(FakeBoard(card()))

    running = asyncio.create_task(poller.run_forever())
    await asyncio.sleep(0.05)
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
