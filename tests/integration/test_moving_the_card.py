"""Dragging an item's board card when its status is set in Discord.

The board has been read and never written since the poller was built. A status set here moved
the labels on GitHub, the stored row and the thread, and left the card sitting in whatever column
it was in - so the one place a reader of the board looks went on saying the old thing.

Two gates stand in front of the WRITE and both are wiring rather than a check: a deployment with
no project token has no writer at all, and `SHANNON_BOARD_MAY_MOVE_CARDS` off empties the writer
too. Neither withholds the board itself, which is issue #179: the flag used to hand the workflow
no board at all, so turning off card writes also turned off the rule that refuses a move the
board's own column order forbids - a read, costing nothing but a read.

What is tested here is that the write happens when both gates are open, that it does not when
either is shut, that the column rules apply either way, and the two cases where there is nothing
to write to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.config import Settings
from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import Priority, Status
from shannon.github.errors import GitHubRefusedError
from shannon.github.projects import BoardOrder, CardMove, CardMoved
from shannon.services.sync.items import ItemSyncService
from shannon.services.workflow import (
    ItemWorkflow,
    WorkflowRefusedError,
    build_item_workflow,
)
from tests.fakes.discord_objects import (
    FakeGuildPermissions,
    FakeInteraction,
    FakeMember,
)
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import SECRET
from tests.support.stack import build_stack

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
        order: BoardOrder | None = None,
        order_error: Exception | None = None,
    ):
        self.error = error
        self.answer = answer
        self.column = column
        # None is a board whose columns could not be read, which is what every test here that
        # is about the WRITE wants: no order means no rule, so nothing is refused before the
        # thing under test happens. `TestTheOrderTheBoardIsIn` sets a real one.
        self.order = order
        self.order_error = order_error
        self.moved: list[tuple[str, int, int, Status | Priority]] = []
        # The board column a command named, where it named one.
        self.named: list[str] = []
        self.asked: list[tuple[Status, Status]] = []

    async def move_card(
        self,
        *,
        owner: str,
        project_number: int,
        card_id: int,
        state: Status | Priority,
        column: str = "",
    ) -> CardMoved:
        self.moved.append((owner, project_number, card_id, state))
        self.named.append(column)
        if self.error is not None:
            raise self.error
        return CardMoved(self.answer, column=self.column)

    async def order_for(
        self, *, owner: str, project_number: int, frm: Status, to: Status, column: str = ""
    ) -> BoardOrder | None:
        self.asked.append((frm, to))
        if self.order_error is not None:
            raise self.order_error
        return self.order


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
        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

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

        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

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
        """No board object at all, which is a service built without one rather than a setting.

        `SHANNON_BOARD_MAY_MOVE_CARDS` used to produce this, and issue #179 is what that cost:
        withholding the object took `order_for` with it, so turning off card WRITES also turned
        off the rule that refuses a move the board forbids. The flag now empties the writer and
        leaves the reader, and what "off" looks like from here is `CardMove.NO_WRITER` - pinned
        in `TestTheOrderTheBoardIsIn` below.
        """
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
            await workflow_with(refusing).set_status(thread_id=thread_id, status=Status.IN_REVIEW)

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


# GitHub's own default template. Every column counts as a step now, because the picker offers
# every column - while `/status` took a fixed list of four names nothing could write to
# `In progress`, and counting it demanded a move nobody could make.
TEMPLATE = ("Backlog", "Ready", "In progress", "In review", "Done")


class TestTheOrderTheBoardIsIn:
    """A card moves forward one column at a time and back as far as you like.

    This replaced a rule written out in Python - a pull request had to be `Ready for merge` before
    `Done` - which named a column GitHub's own default template does not have. The requirement is
    the same and the board is now what states it.
    """

    def order(self, *, leaving: str, arriving: str) -> BoardOrder:
        return BoardOrder(columns=TEMPLATE, leaving=leaving, arriving=arriving)

    async def test_a_skipped_column_is_refused_before_anything_is_written(
        self, on_a_board: None, workflow_with, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Refused first, and that ordering is the point. Everything else on this path - the
        labels, the row, the thread - has already landed by the time the card is touched, so a
        rule applied at the write would report a change it then declined to mirror."""
        cards = FakeCards(order=self.order(leaving="Ready", arriving="Done"))

        with pytest.raises(WorkflowRefusedError, match="In review"):
            await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert cards.moved == [], "it wrote to the board it had just refused"
        assert github.label_calls == [], "a refused command still wrote to GitHub"

    async def test_the_refusal_says_where_to_move_it_instead(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """A bool would leave whoever ran it to open the board and work out which column. The
        names come back so the sentence can name the next one."""
        cards = FakeCards(order=self.order(leaving="Backlog", arriving="Done"))

        with pytest.raises(WorkflowRefusedError) as refused:
            await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        said = refused.value.message
        assert "would skip Ready, In progress, In review" in said
        assert "Move it to Ready first" in said
        # The board's own order, in full, because that is what the board looks like - and every
        # column in it is now a step somebody can take, so naming one is advice they can act on.
        assert "Backlog -> Ready -> In progress -> In review -> Done" in said

    async def test_the_rule_still_applies_when_the_bot_may_not_write(
        self, on_a_board: None, workflow_with, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Issue #179, and the whole of why the flag moved to the writer.

        A deployment that does not want this bot touching its board still wants its board's own
        column order respected - reading it costs a read the bot already makes, and the columns
        are the thing the server set up. With writes off the board answers `NO_WRITER`, which is
        what the flag now produces, and the refusal must land exactly as it does with writes on.

        Before this, "off" meant the workflow was handed no board at all: the order was never
        read, nothing was ever refused, and `/status` wrote a label and called it done.
        """
        cards = FakeCards(
            answer=CardMove.NO_WRITER, order=self.order(leaving="Ready", arriving="Done")
        )

        with pytest.raises(WorkflowRefusedError, match="In review"):
            await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert cards.moved == [], "it tried to write to a board it may not write to"
        assert github.label_calls == [], "a refused command still wrote to GitHub"

    async def test_a_legal_move_with_writes_off_still_lands_everywhere_else(
        self, on_a_board: None, workflow_with, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """The other arm. Writes being off must cost the CARD and nothing else: the label, the row
        and the thread are the parts that do not need anybody's board token."""
        cards = FakeCards(
            answer=CardMove.NO_WRITER, order=self.order(leaving="In review", arriving="Done")
        )

        outcome = await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert outcome.changed is True
        assert github.label_calls, "the label did not land, so this proves nothing"

    async def test_one_step_forward_goes_through(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        cards = FakeCards(order=self.order(leaving="In review", arriving="Done"))

        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert cards.moved != []

    async def test_the_column_a_command_named_reaches_the_board(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """The point of the picker. `In progress` and `In review` both read as IN_REVIEW, so
        without a name to go on the exact-name pass takes `In review` every time and nothing
        could put a card in `In progress` at all."""
        cards = FakeCards(order=self.order(leaving="Ready", arriving="In progress"))

        await workflow_with(cards).set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert cards.named == ["In progress"]

    async def test_a_board_whose_columns_will_not_read_refuses_nothing(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """Fails open, the way every other board read on this path does. A GitHub outage must not
        take `/status` down for everybody."""
        cards = FakeCards(order=None)

        await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert cards.moved != []

    async def test_the_poller_is_never_refused(
        self, on_a_board: None, workflow_with, thread_id: int
    ) -> None:
        """`tell_the_board=False` is exactly the poller, and it calls BECAUSE a card has already
        moved. Somebody dragging one is the fact being mirrored rather than a request to be
        judged, and refusing it would leave the board and the row disagreeing for ever."""
        cards = FakeCards(order=self.order(leaving="Backlog", arriving="Done"))

        await workflow_with(cards).set_status(
            thread_id=thread_id, status=Status.DONE, tell_the_board=False
        )

        assert cards.asked == [], "it asked the board about a move the board had already made"

    async def test_a_board_that_will_not_answer_is_logged_and_lets_the_move_through(
        self, on_a_board: None, workflow_with, thread_id: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rate limit, an outage, a token that stopped working. None of them is a reason to stop
        somebody setting a status, so the read is allowed to fail - but silently failing open would
        leave a rule that quietly stops applying and nothing anywhere saying when.

        Logged with GitHub's own words, because that is the only evidence which of the three it was.
        """
        cards = FakeCards(order_error=GitHubRefusedError("API rate limit exceeded"))

        with caplog.at_level("WARNING", logger="shannon.services.workflow"):
            await workflow_with(cards).set_status(thread_id=thread_id, status=Status.DONE)

        assert cards.moved != [], "a board it could not read stopped the move"
        assert "API rate limit exceeded" in caplog.text
        assert f"board {BOARD}" in caplog.text


# The Status column of GitHub's own default project template, which is what most boards look
# like. Read as JSON rather than as a `BoardOrder`, because the test below goes through the real
# board reader: option ids are strings and a name arrives as a {raw, html} pair, both of which a
# hand-built order would quietly skip. The canonical copy lives in
# `tests/unit/github/test_project_boards.py`.
STATUS_FIELD = {
    "id": 353672864,
    "name": "Status",
    "data_type": "single_select",
    "options": [
        {"id": "f75ad846", "name": {"raw": "Backlog", "html": "Backlog"}, "color": "GREEN"},
        {"id": "e18bf179", "name": {"raw": "Ready", "html": "Ready"}, "color": "BLUE"},
        {
            "id": "47fc9ee4",
            "name": {"raw": "In progress", "html": "In progress"},
            "color": "YELLOW",
        },
        {"id": "aba860b9", "name": {"raw": "In review", "html": "In review"}, "color": "PURPLE"},
        {"id": "98236657", "name": {"raw": "Done", "html": "Done"}, "color": "ORANGE"},
    ],
}


class TestTheWireFromTheContainer:
    """That the workflow is handed a board at all, which nothing else here can see.

    Issue #179 was one line of wiring: `cards=boards if settings.board_may_move_cards else None`.
    Withholding the object did stop the card being written, and also took `order_for` with it - so
    a deployment that merely did not want its board written to lost the rule that refuses a move
    the board's own column order forbids, and `/status` wrote a label and called it done.

    Every other test in this file builds the workflow directly and hands it a board, so none of
    them could see a container that handed it none. `Container` exposes "only the pieces somebody
    outside the wiring asks for by name" and the workflow is not one of them, so this goes the
    long way round: the real container, the real `/status` out of `container.commands`, and the
    real board reader answering out of the fake's JSON.

    With card writes OFF, deliberately. That is the configuration the bug hid in, and the one
    where the rule still has to apply.
    """

    def a_board_github(self, pr_event) -> FakeGitHubClient:
        """The file's own fake, plus the two reads the BOARD half makes.

        The pull request has to be stocked as well: this goes through the whole command, so the
        workflow reads the item from GitHub before it ever asks the board anything.
        """
        github = FakeGitHubClient(pull_requests={REPO_KEY: pr_event("opened")})
        # What the board reader asks for, in the order it asks: the kind of account, then the
        # board's fields. A person's board, so the path prefix is `users`.
        github.bodies[f"/users/{payloads.OWNER}"] = {"type": "User"}
        github.bodies[f"/users/{payloads.OWNER}/projectsV2/{BOARD}/fields"] = [
            {"id": 39516, "name": "Title"},
            STATUS_FIELD,
        ]
        return github

    async def run_status(self, container: object, to: str, thread_id: int) -> FakeInteraction:
        command = next(c for c in container.commands if c.name == "status")  # type: ignore[attr-defined]
        interaction = FakeInteraction(
            channel_id=thread_id,
            user=FakeMember(id=909, roles=[], guild_permissions=FakeGuildPermissions(True)),
        )
        await command.callback(interaction, to)
        return interaction

    async def test_a_skipped_column_is_refused_with_card_writes_off(
        self,
        on_a_board: None,
        db_engine: AsyncEngine,
        threads: FakeThreadGateway,
        thread_id: int,
        pr_event,
    ) -> None:
        """The whole of issue #179, from the end a person touches.

        The item is in Backlog and the board goes Backlog, Ready, In progress, In review, Done -
        so `Done` skips three columns and must be refused. Before the fix the workflow held no
        board, nothing was read, nothing was refused, and the label went on regardless.
        """
        github = self.a_board_github(pr_event)
        container = build_stack(
            db_engine,
            threads=threads,
            github=github,
            # No project token on purpose, so the board reads through the FAKE rather than a
            # real HTTP client: with one set the container gives the board a client of its own
            # and every read here would 401. It costs nothing that matters to this test - with
            # writes off there is no writer either way, and what is under test is whether the
            # board is READ at all.
            settings=Settings(github_webhook_secret=SECRET, board_may_move_cards=False),
        )

        interaction = await self.run_status(container, "Done", thread_id)

        assert "skip" in interaction.reply, (
            f"the board's column order was never consulted: {interaction.reply!r}"
        )
        assert github.label_calls == [], "a refused command still wrote the label to GitHub"

    async def test_a_legal_move_still_goes_through_with_card_writes_off(
        self,
        on_a_board: None,
        db_engine: AsyncEngine,
        threads: FakeThreadGateway,
        thread_id: int,
        pr_event,
    ) -> None:
        """The other arm, so the test above is refusing the right thing rather than everything.
        One column forward is allowed, and the parts that need no board token still land."""
        github = self.a_board_github(pr_event)
        container = build_stack(
            db_engine,
            threads=threads,
            github=github,
            # No project token on purpose, so the board reads through the FAKE rather than a
            # real HTTP client: with one set the container gives the board a client of its own
            # and every read here would 401. It costs nothing that matters to this test - with
            # writes off there is no writer either way, and what is under test is whether the
            # board is READ at all.
            settings=Settings(github_webhook_secret=SECRET, board_may_move_cards=False),
        )

        interaction = await self.run_status(container, "Ready", thread_id)

        assert "skip" not in interaction.reply, f"a legal move was refused: {interaction.reply!r}"
        assert github.label_calls, "the label did not land, so this proves nothing"
