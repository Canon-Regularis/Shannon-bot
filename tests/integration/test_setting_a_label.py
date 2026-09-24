"""Putting an ordinary label on an item, and the two things that are refused.

Issue #104. The mechanism already existed: `_apply` takes any `LabelChange` and the thread's tag
line is already rendered for any name that is neither a status nor a priority. What is new is
saying no, in two directions, and both refusals exist because the failure they prevent is silent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import ObjectType
from shannon.services.sync.items import ItemSyncService
from shannon.services.workflow import (
    ItemWorkflow,
    NotAnItemThreadError,
    WorkflowRefusedError,
    build_item_workflow,
)
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()
REPO_KEY = (REPO_FULL, 7)
HAS = ["bug", "documentation", "good first issue"]


@pytest.fixture
def github(pr_event) -> FakeGitHubClient:
    client = FakeGitHubClient(pull_requests={REPO_KEY: pr_event("opened")})
    client.repo_labels[REPO_FULL] = list(HAS)
    return client


@pytest.fixture
def workflow(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    sync_service: ItemSyncService,
    issue_service: ItemSyncService,
) -> ItemWorkflow:
    return build_item_workflow(
        db_sessionmaker, github, threads, pr_sync=sync_service, issue_sync=issue_service
    )


@pytest.fixture
async def ticket_thread(db_session: AsyncSession, registered: Repository) -> int:
    """A draft card's thread: a tracked item this service has no way to write to.

    Its `github_object_number` is the BOARD's number rather than an item's, which is what the
    poller stores for a card that has no number of its own. That is why the refusal cannot
    simply be dropped.
    """
    db_session.add(
        TrackedItem(
            repository_id=registered.id,
            github_object_id=555,
            github_object_type=ObjectType.TICKET,
            github_object_number=1,
            github_url="",
            title="A card",
            discord_thread_id=7777,
        )
    )
    await db_session.commit()
    return 7777


def added(github: FakeGitHubClient) -> list[str]:
    return [label for kind, _, label in github.label_calls if kind == "add"]


def removed(github: FakeGitHubClient) -> list[str]:
    return [label for kind, _, label in github.label_calls if kind == "remove"]


class TestPuttingOneOn:
    async def test_it_reaches_github(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        outcome = await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

        assert added(github) == ["bug"]
        assert (outcome.changed, outcome.label) == (True, "bug")

    async def test_nothing_comes_off_to_make_room(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Unlike a status or a priority, which are single-valued."""
        await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

        assert removed(github) == []

    async def test_the_repository_s_spelling_is_what_gets_written(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """GitHub matches a label name without regard to case, so writing `BUG` would attach the
        `bug` it already had while the block, comparing case-folded, saw nothing change."""
        outcome = await workflow.set_label(thread_id=thread_id, name="BUG", adding=True)

        assert added(github) == ["bug"]
        assert outcome.label == "bug"

    async def test_one_the_item_already_has_writes_nothing(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, pr_event
    ) -> None:
        from dataclasses import replace

        from shannon.domain.models import Label

        github.pull_requests[REPO_KEY] = replace(pr_event("opened"), labels=(Label(name="bug"),))

        outcome = await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

        assert outcome.changed is False
        assert github.label_calls == []


class TestTakingOneOff:
    async def test_it_reaches_github(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, pr_event
    ) -> None:
        from dataclasses import replace

        from shannon.domain.models import Label

        github.pull_requests[REPO_KEY] = replace(pr_event("opened"), labels=(Label(name="bug"),))

        outcome = await workflow.set_label(thread_id=thread_id, name="bug", adding=False)

        assert removed(github) == ["bug"]
        assert outcome.changed is True

    async def test_one_the_item_does_not_have_writes_nothing(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        outcome = await workflow.set_label(thread_id=thread_id, name="bug", adding=False)

        assert outcome.changed is False
        assert github.label_calls == []


class TestTheNameThisBotOwns:
    """The first refusal. Writing one of these makes the block contradict itself."""

    @pytest.mark.parametrize("name", ["DONE", "done", "IN_REVIEW"])
    async def test_a_status_is_refused_and_says_which_command_owns_it(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, name: str
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="workflow status"):
            await workflow.set_label(thread_id=thread_id, name=name, adding=True)

        assert github.label_calls == []

    async def test_the_refusal_names_the_command_and_the_word_to_pick(
        self, workflow: ItemWorkflow, thread_id: int
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="/status and pick In review"):
            await workflow.set_label(thread_id=thread_id, name="IN_REVIEW", adding=True)

    @pytest.mark.parametrize("name", ["critical", "urgent", "minor", "p-high"])
    async def test_anything_the_priority_parser_reads_is_refused(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, name: str
    ) -> None:
        """The sharper half: these DO feed the stored priority column, so writing one would change
        an item's priority from a command that never mentioned priority."""
        with pytest.raises(WorkflowRefusedError, match="priority here"):
            await workflow.set_label(thread_id=thread_id, name=name, adding=True)

        assert github.label_calls == []

    async def test_the_priority_refusal_names_its_command_and_word(
        self, workflow: ItemWorkflow, thread_id: int
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="/priority and pick Medium"):
            await workflow.set_label(thread_id=thread_id, name="moderate", adding=True)

    async def test_it_is_refused_before_github_is_asked_anything(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        with pytest.raises(WorkflowRefusedError):
            await workflow.set_label(thread_id=thread_id, name="DONE", adding=True)

        assert github.label_list_calls == []


class TestTheNameTheRepositoryDoesNotHave:
    """The second refusal. GitHub creates a label it has never seen rather than refusing, so a
    typo would add one to the repository for good and nothing here could remove it."""

    async def test_an_unknown_name_is_refused(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="no label called"):
            await workflow.set_label(thread_id=thread_id, name="bugg", adding=True)

        assert github.label_calls == []

    async def test_the_refusal_lists_what_the_repository_does_have(
        self, workflow: ItemWorkflow, thread_id: int
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="good first issue"):
            await workflow.set_label(thread_id=thread_id, name="bugg", adding=True)

    async def test_a_repository_with_no_labels_says_so_differently(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        github.repo_labels[REPO_FULL] = []

        with pytest.raises(WorkflowRefusedError, match="no labels at all"):
            await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

    async def test_a_long_taxonomy_is_cut_rather_than_listed_whole(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """The reply is one Discord message, and a repository can have a hundred labels.

        Both halves asserted, because they are computed separately and only one of them was
        pinned: the count came out right while the message went on listing all forty, which a
        mutation of the slice found and this did not.
        """
        github.repo_labels[REPO_FULL] = [f"label-{n:03}" for n in range(40)]

        with pytest.raises(WorkflowRefusedError) as refusal:
            await workflow.set_label(thread_id=thread_id, name="nope", adding=True)

        said = refusal.value.message
        assert "and 25 more" in said
        assert "label-014" in said, "the ones it does list should be the first fifteen"
        assert "label-015" not in said, "everything past the cut is still in the message"
        assert "label-039" not in said


class TestWhatElseItRefuses:
    async def test_a_thread_this_bot_does_not_track(self, workflow: ItemWorkflow) -> None:
        with pytest.raises(NotAnItemThreadError):
            await workflow.set_label(thread_id=999999, name="bug", adding=True)

    async def test_a_project_board_card(self, workflow: ItemWorkflow, ticket_thread: int) -> None:
        with pytest.raises(WorkflowRefusedError, match="no GitHub labels"):
            await workflow.set_label(thread_id=ticket_thread, name="bug", adding=True)

    async def test_a_card_is_told_to_convert_rather_than_to_move_its_card(
        self, workflow: ItemWorkflow, ticket_thread: int
    ) -> None:
        """Status and priority tell you to move the card, which is right for them and wrong here:
        a column is not a label, and moving one sets no label at all. The advice differs per
        command even though the refusal is shared."""
        with pytest.raises(WorkflowRefusedError, match="Convert it to an issue"):
            await workflow.set_label(thread_id=ticket_thread, name="bug", adding=True)

    async def test_a_repository_renamed_away_from_under_it(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient, pr_event
    ) -> None:
        """Refused before the labels are listed, so a stranger's taxonomy is never read back to
        whoever ran the command.

        `WorkflowRefusedError` rather than `RepositoryMismatchError`, which is what the newer
        services raise for the very same condition. This one inherits `ItemWorkflow._fetch`, which
        predates that type. Both reach the reader as their own message, so nothing is wrong in
        front of a user, but the guard now exists in three places under two names.
        """
        from dataclasses import replace

        snapshot = pr_event("opened")
        github.pull_requests[REPO_KEY] = replace(
            snapshot, repository=replace(snapshot.repository, github_repo_id=999999)
        )

        with pytest.raises(WorkflowRefusedError, match="not the repository"):
            await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

        assert github.label_list_calls == []


class TestWhatThePickerSees:
    async def test_it_answers_the_repository_s_labels(
        self, workflow: ItemWorkflow, thread_id: int
    ) -> None:
        assert await workflow.labels_for_thread(thread_id) == tuple(HAS)

    async def test_a_thread_that_is_not_an_item_answers_nothing(
        self, workflow: ItemWorkflow, thread_id: int
    ) -> None:
        """An autocomplete has nowhere to put a refusal, so a channel that is not an item's thread
        gets an empty picker rather than an exception Discord renders as silence anyway."""
        assert await workflow.labels_for_thread(424242) == ()

    async def test_a_project_card_is_offered_nothing_rather_than_everything(
        self, workflow: ItemWorkflow, ticket_thread: int
    ) -> None:
        """The bug this closes. A draft card's thread IS a tracked item, so it got past the guard
        above and was handed every label the registered repository has - a picker that looked
        like it worked, every entry of which `set_label` then refused. Offering nothing is the
        only honest answer while a draft card has nothing to write a label to.
        """
        assert await workflow.labels_for_thread(ticket_thread) == ()


async def test_the_stored_status_and_priority_are_untouched(
    workflow: ItemWorkflow, thread_id: int, db_session: AsyncSession
) -> None:
    """An ordinary label is not a workflow move. This is the invariant the reserved-name refusal
    exists to protect, asserted from the other side."""
    before = await db_session.scalar(select(TrackedItem))
    assert before is not None
    was = (before.status, before.priority)

    await workflow.set_label(thread_id=thread_id, name="bug", adding=True)

    db_session.expire_all()
    after = await db_session.scalar(select(TrackedItem))
    assert after is not None
    assert (after.status, after.priority) == was
