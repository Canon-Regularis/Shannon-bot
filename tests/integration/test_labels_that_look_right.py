"""Issue #246, through the workflow: labels named for what they are, in their colours.

`status: ` and the board's own column, coloured as the board colours it, and `priority: High`
in red. The labels this bot wrote before are renamed in place the first time their new name is
written, so every item carrying one moves at once. And a column this bot has no status for -
`Ready To Merge` - is a move like any other, which keeps the item's status.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import Priority, Status
from shannon.domain.models import TrackedSnapshot
from shannon.github.errors import GitHubNotFoundError, GitHubUnavailableError
from shannon.github.projects import BoardColumn
from shannon.services.sync.items import ItemSyncService
from shannon.services.sync.one_at_a_time import ItemLock
from shannon.services.workflow import (
    FoundItem,
    ItemMovedError,
    ItemWorkflow,
    WorkflowRefusedError,
    build_item_workflow,
)
from tests.fakes.board_credentials import FakeBoardCredentials
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

REPO = f"{payloads.OWNER}/{payloads.REPO}".lower()
PR_KEY = (REPO, 7)
ISSUE_KEY = (REPO, 12)

# A board with one column for each of the statuses these tests move through, and one with none.
ONE_EACH = (
    BoardColumn("Backlog", "GRAY"),
    BoardColumn("In progress", "YELLOW"),
    BoardColumn("Ready To Merge", "ORANGE"),
    BoardColumn("Done", "GREEN"),
)


class FakeBoardColumns:
    """A server's board columns, the way `BoardColumns.columns` answers.

    `cached` is what a read inside the cache's life answers, where a test needs it to lag the
    board; a fresh read always answers `board`.
    """

    def __init__(self, *board: BoardColumn) -> None:
        self.board = board
        self.cached: tuple[BoardColumn, ...] | None = None
        self.error: Exception | None = None
        self.reads: list[bool] = []

    async def columns(self, guild_id: int, *, fresh: bool = False) -> tuple[BoardColumn, ...]:
        self.reads.append(fresh)
        if self.error is not None:
            raise self.error
        if not fresh and self.cached is not None:
            return self.cached
        return self.board


@pytest.fixture
def github(pr_event, issue_event) -> FakeGitHubClient:
    """GitHub holding the two items the payloads describe, keeping the repository's labels."""
    github = FakeGitHubClient(
        pull_requests={PR_KEY: pr_event("opened")},
        issues={ISSUE_KEY: issue_event("opened")},
    )
    github.label_colours_by_repo[REPO] = {"backend": "1d76db"}
    return github


@pytest.fixture
def columns() -> FakeBoardColumns:
    return FakeBoardColumns(*ONE_EACH)


@pytest.fixture
def workflow(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    sync_service: ItemSyncService,
    issue_service: ItemSyncService,
    columns: FakeBoardColumns,
) -> ItemWorkflow:
    return build_item_workflow(
        db_sessionmaker,
        github,
        threads,
        pr_sync=sync_service,
        issue_sync=issue_service,
        authorisations=FakeBoardCredentials(),
        columns=columns,
    )


@pytest.fixture
async def issue_thread_id(
    registered: Repository, issue_service: ItemSyncService, issue_event
) -> int:
    result = await issue_service.sync(issue_event("opened"))
    assert result.thread_id is not None
    return result.thread_id


async def stored(session: AsyncSession) -> TrackedItem:
    session.expire_all()
    item = await session.scalar(select(TrackedItem).order_by(TrackedItem.id))
    assert item is not None
    return item


class TestAStatusLabel:
    async def test_it_is_the_boards_column_in_the_boards_colour(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Spelled as the board has it, whatever was typed."""
        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="in progress"
        )

        assert github.labels[PR_KEY] == ["backend", "status: In progress"]
        assert github.label_colours_by_repo[REPO]["status: In progress"] == "d4a72c"

    @pytest.mark.parametrize("typed", ["in-progress", "In_Progress", "in  progress"])
    async def test_it_is_matched_the_way_the_card_is(
        self, typed: str, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """The card write reads `in-progress` as `In progress`, so the label has to as well."""
        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW, column=typed)

        assert github.labels[PR_KEY] == ["backend", "status: In progress"]

    async def test_a_status_the_board_calls_by_another_name_is_labelled_as_its_column(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """`In review` is not on this board, and the card lands in `In progress`, the board's
        column for it - so that is what the label says, rather than a column the board lacks."""
        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW, column="In review")

        assert github.labels[PR_KEY] == ["backend", "status: In progress"]

    async def test_with_no_column_named_it_is_the_boards_column_for_the_status(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)

        assert github.labels[PR_KEY] == ["backend", "status: Backlog"]
        assert github.label_colours_by_repo[REPO]["status: Backlog"] == "8b949e"

    async def test_a_status_the_board_has_no_column_for_is_one_of_this_bots_own(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """This board has no column for NOT_REVIEWED, so the card stays put and the label is the
        bot's own name for the status, in its own colour."""
        await workflow.set_status(thread_id=thread_id, status=Status.NOT_REVIEWED)

        assert github.labels[PR_KEY] == ["backend", "status: Not reviewed"]
        assert github.label_colours_by_repo[REPO]["status: Not reviewed"] == "0969da"

    async def test_with_no_board_at_all_it_is_one_of_this_bots_own(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
    ) -> None:
        columns.board = ()

        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert github.labels[PR_KEY] == ["backend", "status: In review"]

    async def test_moving_on_takes_the_last_one_off(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )
        await workflow.set_status(thread_id=thread_id, status=Status.DONE, column="Done")

        assert github.labels[PR_KEY] == ["backend", "status: Done"]

    async def test_the_thread_shows_the_label_it_was_given(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert "status: In progress" in threads.metadata_of(thread_id)

    async def test_a_column_added_since_the_cache_was_filled_is_read_again(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
    ) -> None:
        """Exactly the column somebody has just picked or dragged a card into."""
        columns.cached = ONE_EACH
        columns.board = (*ONE_EACH, BoardColumn("Doing", "PINK"))

        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW, column="Doing")

        assert github.labels[PR_KEY] == ["backend", "status: Doing"]
        assert github.label_colours_by_repo[REPO]["status: Doing"] == "e85aad"

    async def test_a_column_renamed_since_the_cache_was_filled_is_not_labelled(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
    ) -> None:
        """The cache still has `In progress`; the board calls it `Doing` now, and the card write,
        which reads fresh, lands there. The label has to name the column the card is in."""
        columns.cached = ONE_EACH
        columns.board = (
            BoardColumn("Backlog", "GRAY"),
            BoardColumn("Doing", "PINK"),
            BoardColumn("Done", "GREEN"),
        )

        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert github.labels[PR_KEY] == ["backend", "status: Doing"]
        assert columns.reads == [True], "the label was read from the cache"


class TestABoardThatWillNotRead:
    """The move goes ahead, and the label is the name it was given, put on as it is: the bot
    does not rename or recolour a repository's label on a guess."""

    async def test_the_label_is_the_name_given(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        columns.error = GitHubUnavailableError("GitHub is down")

        with caplog.at_level(logging.WARNING):
            outcome = await workflow.set_status(
                thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
            )

        assert outcome.changed is True
        assert github.labels[PR_KEY] == ["backend", "status: In progress"]
        assert github.definition_calls == [], "a label was defined from a guess"
        assert "could not read the board's columns" in caplog.text

    async def test_the_boards_own_label_is_not_respelled_across_the_repository(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
        issue_thread_id: int,
    ) -> None:
        """Typed in lower case while the board is down. GitHub attaches the label the repository
        already has, and nothing renames it to the typed spelling on every item carrying it."""
        github.label_colours_by_repo[REPO]["status: In progress"] = "d4a72c"
        await github.add_label(payloads.OWNER, payloads.REPO, 12, "status: In progress")
        columns.error = GitHubUnavailableError("GitHub is down")

        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="in progress"
        )

        assert github.definition_calls == []
        assert github.labels[ISSUE_KEY][-1] == "status: In progress"
        assert github.label_colours_by_repo[REPO]["status: In progress"] == "d4a72c"

    async def test_a_board_whose_status_field_will_not_read_is_no_ground_for_a_rename(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
        issue_thread_id: int,
    ) -> None:
        """What `status_columns` raises for a board with no Status field. Answered as no board,
        this renamed `IN_REVIEW` on every item on a guess."""
        columns.error = GitHubNotFoundError("board 6 has no Status field this bot can read")
        await github.add_label(payloads.OWNER, payloads.REPO, 12, "IN_REVIEW")

        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW, column="In review")

        assert github.definition_calls == []
        assert github.labels[ISSUE_KEY][-1] == "IN_REVIEW"

    async def test_with_no_column_named_it_is_this_bots_own_name(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
    ) -> None:
        columns.error = GitHubUnavailableError("GitHub is down")

        await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)

        assert github.labels[PR_KEY] == ["backend", "status: Backlog"]
        assert github.definition_calls == []


class TestAColumnWithNoStatus:
    async def test_it_moves_the_label_and_keeps_the_status(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
    ) -> None:
        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        outcome = await workflow.set_status(
            thread_id=thread_id, status=None, column="Ready To Merge"
        )

        assert outcome.changed is True
        assert github.labels[PR_KEY] == ["backend", "status: Ready To Merge"]
        assert github.label_colours_by_repo[REPO]["status: Ready To Merge"] == "fb8500"
        assert (await stored(db_session)).status is Status.IN_REVIEW

    async def test_asking_again_is_a_repeat(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")
        before = list(github.label_calls)

        outcome = await workflow.set_status(
            thread_id=thread_id, status=None, column="Ready To Merge"
        )

        assert outcome.changed is False
        assert github.label_calls == before

    async def test_an_item_finished_meanwhile_is_left_done(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Somebody finished it after the Done refusal had looked. It stays Done, and the person
        is told rather than answered as though the column move had landed."""
        fetch = workflow._fetch  # pyright: ignore[reportPrivateUsage]

        async def finished_meanwhile(found: FoundItem) -> TrackedSnapshot:
            snapshot = await fetch(found)
            await db_session.execute(
                update(TrackedItem)
                .where(TrackedItem.id == found.tracked_item_id)
                .values(status=Status.DONE)
            )
            await db_session.commit()
            return snapshot

        monkeypatch.setattr(workflow, "_fetch", finished_meanwhile)

        with pytest.raises(ItemMovedError, match="somebody finished it"):
            await workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")

        assert (await stored(db_session)).status is Status.DONE

    async def test_a_repeat_that_finds_the_item_moved_names_the_column_asked_for(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A column, not a status: what was asked for is to be in `Ready To Merge`."""
        await workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")
        tracked = await stored(db_session)
        object_id, row_id = tracked.github_object_id, tracked.id
        holding, release, read_it = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def somebody_else_has_the_item() -> None:
            async with ItemLock(db_sessionmaker).held(object_id):
                holding.set()
                await release.wait()

        reading = github.get_pull_request

        async def says_when_it_has_been_read(owner: str, name: str, number: int):
            found = await reading(owner, name, number)
            read_it.set()
            return found

        monkeypatch.setattr(github, "get_pull_request", says_when_it_has_been_read)
        blocker = asyncio.create_task(somebody_else_has_the_item())
        await asyncio.wait_for(holding.wait(), timeout=5)
        repeat = asyncio.create_task(
            workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")
        )
        await asyncio.wait_for(read_it.wait(), timeout=5)
        async with db_sessionmaker() as session, session.begin():
            moved = await TrackedItemStore(session).get_by_id(row_id)
            assert moved is not None
            moved.status = Status.BACKLOG

        release.set()
        await blocker
        with pytest.raises(ItemMovedError, match="still want it in Ready To Merge"):
            await repeat

    async def test_it_keeps_the_status_the_row_has_when_it_writes(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not the one read before the GitHub round trips. Somebody moving the item in between
        would otherwise have their move undone by a request that changes no status at all."""
        fetch = workflow._fetch  # pyright: ignore[reportPrivateUsage]

        async def moved_meanwhile(found: FoundItem) -> TrackedSnapshot:
            snapshot = await fetch(found)
            await db_session.execute(
                update(TrackedItem)
                .where(TrackedItem.id == found.tracked_item_id)
                .values(status=Status.BACKLOG)
            )
            await db_session.commit()
            return snapshot

        monkeypatch.setattr(workflow, "_fetch", moved_meanwhile)

        await workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")

        assert (await stored(db_session)).status is Status.BACKLOG

    async def test_a_closed_issue_is_refused_one(
        self,
        workflow: ItemWorkflow,
        github: FakeGitHubClient,
        registered: Repository,
        issue_service: ItemSyncService,
        issue_event,
    ) -> None:
        """Closed is Done, and a closed issue labelled `status: Ready To Merge` would be Done in
        Discord and not on GitHub."""
        closed = issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")
        github.issues[ISSUE_KEY] = closed
        result = await issue_service.sync(closed)
        assert result.thread_id is not None

        with pytest.raises(WorkflowRefusedError, match="Reopen it there"):
            await workflow.set_status(
                thread_id=result.thread_id, status=None, column="Ready To Merge"
            )

        assert github.label_calls == []

    async def test_a_pull_request_at_done_is_refused_one(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
    ) -> None:
        """It would stay Done, its thread shut, under a label and a card saying otherwise."""
        await workflow.set_status(thread_id=thread_id, status=Status.DONE, column="Done")
        before = list(github.label_calls)

        with pytest.raises(WorkflowRefusedError, match="Move it to a status first"):
            await workflow.set_status(thread_id=thread_id, status=None, column="Ready To Merge")

        assert github.label_calls == before
        assert threads.threads[thread_id].locked is True

    async def test_one_too_long_for_a_github_label_is_refused(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
    ) -> None:
        long = "Waiting on the design review from the other team"
        columns.board = (BoardColumn(long, "RED"),)

        with pytest.raises(WorkflowRefusedError, match="50 at most"):
            await workflow.set_status(thread_id=thread_id, status=None, column=long)

        assert github.label_calls == []

    async def test_one_the_board_does_not_have_keeps_its_name_unsettled(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Not even on a fresh read. Only a board hands one over, so this is a caller naming a
        column it no longer has: the label goes on as named, and nothing is defined from it."""
        await workflow.set_status(thread_id=thread_id, status=None, column="Shipping soon")

        assert github.labels[PR_KEY] == ["backend", "status: Shipping soon"]
        assert github.definition_calls == []


class TestTheRenameInPlace:
    async def test_the_old_priority_label_becomes_the_new_one_everywhere(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        issue_thread_id: int,
    ) -> None:
        """The issue is never touched, and reads `priority: High` all the same."""
        await github.add_label(payloads.OWNER, payloads.REPO, 7, "HIGH")
        await github.add_label(payloads.OWNER, payloads.REPO, 12, "HIGH")

        outcome = await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        assert outcome.changed is True
        assert github.labels[PR_KEY] == ["backend", "priority: High"]
        assert "HIGH" not in github.labels[ISSUE_KEY]
        assert "priority: High" in github.labels[ISSUE_KEY]
        assert github.label_colours_by_repo[REPO] == {
            "backend": "1d76db",
            "priority: High": "d73a4a",
        }

    async def test_the_old_status_label_where_one_column_claims_it(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, issue_thread_id: int
    ) -> None:
        await github.add_label(payloads.OWNER, payloads.REPO, 12, "IN_REVIEW")

        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert github.labels[ISSUE_KEY][-1] == "status: In progress"
        assert github.definition_calls == [
            ("update", REPO, "IN_REVIEW", "status: In progress", "d4a72c")
        ]

    async def test_not_where_two_columns_could_claim_it(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        columns: FakeBoardColumns,
        issue_thread_id: int,
    ) -> None:
        """GitHub's own template has `In progress` and `In review`. Renaming `IN_REVIEW` to
        whichever was written first would relabel items whose cards are in the other one."""
        columns.board = (*ONE_EACH, BoardColumn("In review", "PURPLE"))
        await github.add_label(payloads.OWNER, payloads.REPO, 12, "IN_REVIEW")

        await workflow.set_status(
            thread_id=thread_id, status=Status.IN_REVIEW, column="In progress"
        )

        assert github.labels[ISSUE_KEY][-1] == "IN_REVIEW", "another item's label was renamed"
        assert github.definition_calls == [("create", REPO, "status: In progress", "", "d4a72c")]

    async def test_a_rename_landing_mid_change_does_not_leave_two_priorities(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another item's write renames `HIGH` while this one is taking it off: the label is
        then on this item as `priority: High`, and comes off too."""
        await github.add_label(payloads.OWNER, payloads.REPO, 7, "HIGH")
        remove = github.remove_label

        async def renamed_under_it(owner: str, name: str, number: int, label: str) -> None:
            if label == "HIGH":
                await github.update_label(
                    owner, name, "HIGH", new_name="priority: High", colour="d73a4a"
                )
            await remove(owner, name, number, label)

        monkeypatch.setattr(github, "remove_label", renamed_under_it)

        await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)

        assert github.labels[PR_KEY] == ["backend", "priority: Low"]

    async def test_a_repeated_priority_converges_and_writes_nothing(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """The bug the new names would have shipped with: every run took the label off and put
        it back."""
        await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)
        before = list(github.label_calls)

        outcome = await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)

        assert outcome.changed is False
        assert github.label_calls == before
        assert github.label_colours_by_repo[REPO]["priority: Low"] == "fde047"


class TestAFailureMakingTheLabel:
    async def test_the_label_still_goes_on(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Grey, as GitHub makes a label nobody defined, which is how it always was."""

        async def unreadable(owner: str, name: str) -> dict[str, str]:
            raise GitHubUnavailableError("GitHub is down")

        monkeypatch.setattr(github, "label_colours", unreadable)

        with caplog.at_level(logging.WARNING):
            await workflow.set_priority(thread_id=thread_id, priority=Priority.MEDIUM)

        assert github.labels[PR_KEY] == ["backend", "priority: Medium"]
        assert github.label_colours_by_repo[REPO]["priority: Medium"] == "ededed"
        assert "will make it grey" in caplog.text
