"""Dragging an item's board card when its status is set in Discord.

The board has been read and never written since the poller was built. A status set here moved
the labels on GitHub, the stored row and the thread, and left the card sitting in whatever column
it was in - so the one place a reader of the board looks went on saying the old thing.

Two gates stand in front of this and both are wiring rather than a check: a deployment with no
project token has no writer at all, and one with `SHANNON_BOARD_MAY_MOVE_CARDS` off is handed no
card mover. What is tested here is that the write happens when both are open, that it does not
when either is shut, and the two cases where there is nothing to write to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import Priority, Status
from shannon.github.errors import GitHubRefusedError
from shannon.github.projects import CardMove, CardMoved
from shannon.services.sync.items import ItemSyncService
from shannon.services.workflow import ItemWorkflow, build_item_workflow
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

BOARD = 6
CARD = 74106766
REPO_KEY = (f"{payloads.OWNER}/{payloads.REPO}".lower(), 7)


@pytest.fixture
def github(pr_event) -> FakeGitHubClient:
    return FakeGitHubClient(pull_requests={REPO_KEY: pr_event("opened")})


class FakeCards:
    """The board's write half, which `ItemWorkflow` sees through a Protocol of one method."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        answer: CardMove = CardMove.MOVED,
        column: str = "In review",
    ):
        self.error = error
        self.answer = answer
        self.column = column
        self.moved: list[tuple[str, int, int, Status | Priority]] = []

    async def move_card(
        self, *, owner: str, project_number: int, card_id: int, state: Status | Priority
    ) -> CardMoved:
        self.moved.append((owner, project_number, card_id, state))
        if self.error is not None:
            raise self.error
        return CardMoved(self.answer, column=self.column)


@pytest.fixture
def cards() -> FakeCards:
    return FakeCards()


@pytest.fixture
def workflow_with(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    sync_service: ItemSyncService,
    issue_service: ItemSyncService,
):
    def build(cards: FakeCards | None) -> ItemWorkflow:
        return build_item_workflow(
            db_sessionmaker,
            github,
            threads,
            pr_sync=sync_service,
            issue_sync=issue_service,
            cards=cards,
        )

    return build


@pytest.fixture
async def on_a_board(registered: Repository, db_session: AsyncSession, thread_id: int) -> None:
    """A repository with a board linked, and an item the poller has paired with a card."""
    registered.project_number = BOARD
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    item.project_item_id = CARD
    await db_session.commit()


class TestWhenBothGatesAreOpen:
    async def test_the_card_is_dragged_to_match(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert cards.moved == [("Canon-Regularis", BOARD, CARD, Status.IN_REVIEW)]

    async def test_the_owner_falls_back_to_the_repositorys_own(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        """Never empty. An empty owner sends the write out with no credential at all, GitHub
        answers 401, and the poller reads that as permanent and writes the card off for good."""
        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)

        assert cards.moved[0][0] == "Canon-Regularis"

    async def test_an_owner_somewhere_else_is_used_instead(
        self,
        on_a_board: None,
        workflow_with,
        cards: FakeCards,
        thread_id: int,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        registered.project_owner = "acme"
        await db_session.commit()

        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)

        assert cards.moved[0][0] == "acme"

    async def test_the_status_change_still_landed(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int, db_session
    ) -> None:
        outcome = await workflow_with(cards).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        assert outcome.changed is True
        db_session.expire_all()
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None and item.status is Status.IN_REVIEW


class TestWhenThereIsNothingToWriteTo:
    async def test_no_card_mover_writes_nothing(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        """Which is `SHANNON_BOARD_MAY_MOVE_CARDS` off: the container passes None, so the write
        cannot be reached rather than being skipped by a check."""
        outcome = await workflow_with(None).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert outcome.changed is True
        assert cards.moved == []

    async def test_a_repository_with_no_board_writes_nothing(
        self, workflow_with, cards: FakeCards, thread_id: int, registered: Repository
    ) -> None:
        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert cards.moved == []

    async def test_an_item_whose_card_has_never_been_seen_writes_nothing(
        self,
        workflow_with,
        cards: FakeCards,
        thread_id: int,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """The board is linked but no poll has paired this item with a card yet, and GitHub
        answers no per-item project lookup - so there is no id to write to and no way to find
        one. The board stays wrong until a poll sees that card."""
        registered.project_number = BOARD
        await db_session.commit()

        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert cards.moved == []


class TestWhenTheBoardRefuses:
    async def test_the_command_still_reports_what_happened(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """Everything that matters landed before the board was asked: the labels are on GitHub,
        the row is written and the thread is redrawn. Telling whoever ran it that the command
        failed would report the opposite of the truth."""
        refusing = FakeCards(error=GitHubRefusedError("Could not resolve to a node"))

        outcome = await workflow_with(refusing).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        assert outcome.changed is True

    async def test_it_says_so_with_githubs_own_words(
        self, on_a_board: None, workflow_with, thread_id: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 422 is the one worth reading. This write's body shape came from published
        documentation rather than from a live board, so GitHub's message is the first real
        evidence either way."""
        refusing = FakeCards(error=GitHubRefusedError("Could not resolve to a node"))

        with caplog.at_level("WARNING", logger="shannon.services.workflow"):
            await workflow_with(refusing).set_status(
                thread_id=thread_id, status=Status.READY_FOR_MERGE
            )

        assert "Could not resolve to a node" in caplog.text
        assert f"board {BOARD}" in caplog.text


class TestThePriorityHalf:
    """`/status` dragged the card and `/priority` did not, although a real board carries a
    Priority field whose options match this bot's own. The asymmetry was the discrepancy."""

    async def test_setting_a_priority_moves_the_card(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        await workflow_with(cards).set_priority(thread_id=thread_id, priority=Priority.HIGH)

        assert cards.moved == [("Canon-Regularis", BOARD, CARD, Priority.HIGH)]

    async def test_the_priority_change_still_landed(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        outcome = await workflow_with(cards).set_priority(
            thread_id=thread_id, priority=Priority.HIGH
        )

        assert outcome.changed is True

    async def test_a_repeat_asks_the_board_again(
        self, on_a_board: None, workflow_with, cards: FakeCards, thread_id: int
    ) -> None:
        """The half with nothing else to retry it. A swallowed board write is permanent - the
        poller only ever rederives a status FROM a column, so a card left behind because GitHub
        was having a moment is noticed by nothing. Running the command again is what a person
        does, and it used to report "already High priority" and ask the board nothing."""
        service = workflow_with(cards)
        await service.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        outcome = await service.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        assert outcome.changed is False
        assert len(cards.moved) == 2, "the repeat asked the board nothing"


class TestWhenTheBoardHasNoColumnForIt:
    """The one board refusal a person is told about, because it is the one they can see: they
    will open the board and find the card where it was."""

    async def test_the_outcome_says_so(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        nowhere = FakeCards(answer=CardMove.NO_COLUMN)

        outcome = await workflow_with(nowhere).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        assert outcome.changed is True
        assert outcome.board_has_no_column is True

    @pytest.mark.parametrize(
        "answer", [CardMove.MOVED, CardMove.NO_WRITER, CardMove.UNREADABLE, CardMove.NO_FIELD]
    )
    async def test_every_other_answer_says_nothing(
        self, on_a_board: None, workflow_with, thread_id: int, answer: CardMove
    ) -> None:
        """No token and a board that cannot be read are invisible and identical for every
        command until an operator changes something. A warning attached to a fix the caller
        cannot make is noise."""
        outcome = await workflow_with(FakeCards(answer=answer)).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        assert outcome.board_has_no_column is False

    async def test_a_refused_write_says_nothing_either(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """A 422 goes to the log with GitHub's words. It is not the caller's to fix and not
        something they can see on the board, since the card is where it always was."""
        refusing = FakeCards(error=GitHubRefusedError("Could not resolve to a node"))

        outcome = await workflow_with(refusing).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        assert outcome.board_has_no_column is False


class TestRememberingWhereItPutIt:
    """A card write that does not record where it landed is read back as somebody else's drag.

    The poller compares the column it last saw, by text. Leave that stale and the next poll sees
    a card whose column disagrees with the memory - the bot's own write, read as a move. Worse,
    a real drag BACK to the old column inside one poll interval reads as never having moved and
    is dropped for good, which is the exact failure `_remember_column` exists to prevent.
    """

    async def test_the_column_the_board_calls_it_is_written_down(
        self, on_a_board: None, workflow_with, thread_id: int, db_session: AsyncSession
    ) -> None:
        """The BOARD's spelling, not the state that was asked for. A board may call IN_REVIEW
        anything this bot reads as it - `In progress`, `Doing` - and storing the wrong one
        disagrees with the board for ever rather than for one poll."""
        await workflow_with(FakeCards(column="In progress")).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        db_session.expire_all()
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None
        assert item.project_column == "In progress"

    async def test_a_priority_write_records_nothing_about_the_column(
        self, on_a_board: None, workflow_with, thread_id: int, db_session: AsyncSession
    ) -> None:
        """The stored column is the STATUS column. A priority write lands in a different field
        entirely, so writing its option name there would tell the poller the card had moved to a
        column called `HIGH`."""
        await workflow_with(FakeCards(column="HIGH")).set_priority(
            thread_id=thread_id, priority=Priority.HIGH
        )

        db_session.expire_all()
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None
        assert item.project_column != "HIGH"

    @pytest.mark.parametrize(
        "answer", [CardMove.NO_WRITER, CardMove.UNREADABLE, CardMove.NO_FIELD, CardMove.NO_COLUMN]
    )
    async def test_nothing_is_written_down_when_nothing_moved(
        self,
        on_a_board: None,
        workflow_with,
        thread_id: int,
        db_session: AsyncSession,
        answer: CardMove,
    ) -> None:
        """The card is where it always was, so the memory of it must not move either."""
        started_as = await db_session.scalar(select(TrackedItem))
        assert started_as is not None
        before = started_as.project_column

        await workflow_with(FakeCards(answer=answer)).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW
        )

        db_session.expire_all()
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None and item.project_column == before
