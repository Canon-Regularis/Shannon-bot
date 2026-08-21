"""Setting an item's status and priority from its thread.

The requirements are specific about two things here and both are tested rather than assumed:
GitHub is written before Discord, and a repeat of a command takes no action. The rest is the
ordinary shape of a command that has to leave two systems agreeing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.enums import Priority, Status
from shannon.domain.errors import ItemNotReadyError
from shannon.github.errors import GitHubUnavailableError
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

REPO_KEY = (f"{payloads.OWNER}/{payloads.REPO}".lower(), 7)
ISSUE_KEY = (f"{payloads.OWNER}/{payloads.REPO}".lower(), 12)


@pytest.fixture
def github(pr_event, issue_event) -> FakeGitHubClient:
    """GitHub holding the same two items the webhook payloads describe."""
    return FakeGitHubClient(
        pull_requests={REPO_KEY: pr_event("opened")},
        issues={ISSUE_KEY: issue_event("opened")},
    )


@pytest.fixture
def workflow(
    db_sessionmaker: async_sessionmaker,
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    sync_service: ItemSyncService,
    issue_service: ItemSyncService,
) -> ItemWorkflow:
    return build_item_workflow(
        db_sessionmaker, github, threads, pr_sync=sync_service, issue_sync=issue_service
    )


@pytest.fixture
async def thread_id(registered: Repository, sync_service: ItemSyncService, pr_event) -> int:
    """A pull request already mirrored, which is where these commands are run."""
    result = await sync_service.sync(pr_event("opened"))
    assert result.thread_id is not None
    return result.thread_id


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


class TestSettingAStatus:
    async def test_it_labels_github_and_records_the_status(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
    ) -> None:
        outcome = await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert outcome.changed is True
        assert github.labels[REPO_KEY] == ["backend", "IN_REVIEW"]
        assert (await stored(db_session)).status is Status.IN_REVIEW

    async def test_github_is_written_before_discord(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The requirement, and the only order that is recoverable.

        GitHub refusing has to leave Discord untouched, or the thread claims a status the
        repository never agreed to and nothing later corrects it.

        The refusal is on the label WRITE, not on everything. Failing every call fails the read
        that comes first, at which point nothing has been attempted and the order this is named
        for is never exercised: the same test passed with the write moved after the re-render.
        """
        github.write_error = GitHubUnavailableError("GitHub is down")
        before = threads.metadata_of(thread_id)

        with pytest.raises(GitHubUnavailableError):
            await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert github.label_calls, "it did not get as far as writing a label"
        assert threads.metadata_of(thread_id) == before, "Discord was told about a refused write"
        assert threads.locks == []
        assert (await stored(db_session)).status is Status.NOT_REVIEWED

    async def test_moving_a_pull_request_out_of_done_gives_its_thread_back(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """Nothing else was ever going to. `PullRequestPolicy.locked` returns None on every sync,
        so the lock `/set_done` takes is the only one a pull request gets, and every command to
        move it back out of DONE wrote the label, moved the stored status, reported success and
        left the thread shut against the discussion it had just reopened.
        """
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)
        await workflow.set_status(thread_id=thread_id, status=Status.DONE)
        assert threads.threads[thread_id].locked is True

        outcome = await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert outcome.locked is False
        assert threads.threads[thread_id].locked is False, "it stayed shut"

    async def test_a_status_change_with_no_done_on_either_side_leaves_the_lock_alone(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """Giving a thread back is worth a call to Discord; saying nothing changed is not."""
        before = list(threads.locks)

        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert threads.locks == before

    async def test_a_failed_write_leaves_the_stored_status_alone(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
    ) -> None:
        github.error = GitHubUnavailableError("GitHub is down")

        with pytest.raises(GitHubUnavailableError):
            await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert (await stored(db_session)).status is Status.NOT_REVIEWED

    async def test_moving_between_statuses_removes_the_one_before(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Spelled out in the requirements: BACKLOG then NOT_REVIEWED undoes the first."""
        await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)

        await workflow.set_status(thread_id=thread_id, status=Status.NOT_REVIEWED)

        assert github.labels[REPO_KEY] == ["backend", "NOT_REVIEWED"]

    async def test_a_repeated_command_takes_no_action(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)
        writes = len(github.label_calls)

        outcome = await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)

        assert outcome.changed is False
        assert len(github.label_calls) == writes, "a repeat wrote to GitHub again"

    async def test_the_thread_metadata_is_brought_in_line(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        assert (
            "IN_REVIEW"
            in threads.threads[thread_id].messages[threads.threads[thread_id].metadata_message_id]
        )

    async def test_a_thread_that_belongs_to_nothing_is_refused(
        self, workflow: ItemWorkflow, registered: Repository
    ) -> None:
        with pytest.raises(NotAnItemThreadError):
            await workflow.set_status(thread_id=424242, status=Status.IN_REVIEW)


class TestFinishing:
    """`/set_done` locks the thread, and only once a reviewer has said it may be merged."""

    async def test_a_pull_request_has_to_be_ready_for_merge_first(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        with pytest.raises(WorkflowRefusedError, match="READY_FOR_MERGE"):
            await workflow.set_status(thread_id=thread_id, status=Status.DONE)

        assert github.label_calls == [], "a refused command still wrote to GitHub"

    async def test_once_it_is_ready_it_can_be_finished_and_the_thread_locks(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)

        outcome = await workflow.set_status(thread_id=thread_id, status=Status.DONE)

        assert outcome.locked is True
        assert threads.threads[thread_id].locked is True

    async def test_the_metadata_is_written_before_the_lock(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """A locked thread rejects edits, so locking first leaves the block saying IN_REVIEW."""
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)

        await workflow.set_status(thread_id=thread_id, status=Status.DONE)

        thread = threads.threads[thread_id]
        assert "DONE" in thread.messages[thread.metadata_message_id]

    async def test_an_open_issue_is_sent_to_close_it_instead(
        self, workflow: ItemWorkflow, issue_thread_id: int
    ) -> None:
        """The requirements defer the issue side and say closing is what marks one done, which
        the sync path already does. Setting it by hand would be undone by the next delivery,
        because reopening an issue is what clears DONE."""
        with pytest.raises(WorkflowRefusedError, match="Close the issue"):
            await workflow.set_status(thread_id=issue_thread_id, status=Status.DONE)


class TestSettingAPriority:
    async def test_it_labels_github_and_shows_in_the_thread(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        outcome = await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        assert outcome.changed is True
        assert "HIGH" in github.labels[REPO_KEY]

    async def test_the_stored_priority_follows_the_label(
        self, workflow: ItemWorkflow, thread_id: int, db_session: AsyncSession
    ) -> None:
        """The re-render reads priority off the snapshot's labels, so this is what proves the
        change was carried forward rather than the pre-write snapshot being replayed."""
        await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        assert (await stored(db_session)).priority is Priority.HIGH

    async def test_moving_priority_removes_the_one_before(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)

        assert "HIGH" not in github.labels[REPO_KEY]
        assert "LOW" in github.labels[REPO_KEY]

    async def test_a_repeated_command_takes_no_action(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)
        writes = len(github.label_calls)

        outcome = await workflow.set_priority(thread_id=thread_id, priority=Priority.LOW)

        assert outcome.changed is False
        assert len(github.label_calls) == writes

    async def test_the_status_is_left_where_it_was(
        self, workflow: ItemWorkflow, thread_id: int, db_session: AsyncSession
    ) -> None:
        await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)

        await workflow.set_priority(thread_id=thread_id, priority=Priority.HIGH)

        item = await stored(db_session)
        assert item.status is Status.IN_REVIEW
        assert item.priority is Priority.HIGH

    async def test_it_works_on_an_issue_too(
        self, workflow: ItemWorkflow, issue_thread_id: int, github: FakeGitHubClient
    ) -> None:
        await workflow.set_priority(thread_id=issue_thread_id, priority=Priority.MEDIUM)

        assert "MEDIUM" in github.labels[ISSUE_KEY]


class TestTheAwkwardOnes:
    """Three paths that only exist because two systems and a gap between them do."""

    async def test_a_closed_issue_can_be_marked_done(
        self,
        registered: Repository,
        workflow: ItemWorkflow,
        issue_service: ItemSyncService,
        github: FakeGitHubClient,
        issue_event,
    ) -> None:
        """Closing sets the stored status but leaves GitHub unlabelled, so there is still work.

        The two directions are deliberately not symmetric. Mirroring reads GitHub and writes
        Discord; nothing on that path writes back, because a label this bot wrote would arrive
        as a `labeled` delivery, sync, and be written again. So a closed issue reads DONE here
        and carries no DONE tag there until somebody asks for one.
        """
        closed = issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")
        github.issues[ISSUE_KEY] = closed
        result = await issue_service.sync(closed)
        assert result.thread_id is not None

        outcome = await workflow.set_status(thread_id=result.thread_id, status=Status.DONE)

        assert outcome.changed is True
        assert "DONE" in github.labels[ISSUE_KEY]

    async def test_an_item_wearing_two_statuses_is_tidied_without_a_write(
        self, workflow: ItemWorkflow, thread_id: int, github: FakeGitHubClient
    ) -> None:
        """Somebody labelling by hand can leave both on. Setting one it already has removes the
        other and adds nothing, which is a change with no addition in it."""
        github.set_labels(REPO_KEY, ["BACKLOG", "IN_REVIEW"])

        await workflow.set_status(thread_id=thread_id, status=Status.BACKLOG)

        assert github.labels[REPO_KEY] == ["BACKLOG"]
        assert [call[0] for call in github.label_calls] == ["remove"], "it wrote a label it had"

    async def test_an_item_unregistered_mid_command_is_reported(
        self,
        workflow: ItemWorkflow,
        thread_id: int,
        github: FakeGitHubClient,
        db_sessionmaker: async_sessionmaker,
    ) -> None:
        """GitHub is written before the stored copy, so the row can go in between. Deleted from
        inside the label write, which is where that gap actually is."""
        original = github.add_label

        async def unregister_mid_write(owner: str, name: str, number: int, label: str) -> None:
            await original(owner, name, number, label)
            async with db_sessionmaker() as session, session.begin():
                await session.execute(delete(Repository))

        github.add_label = unregister_mid_write

        with pytest.raises(ItemNotReadyError):
            await workflow.set_status(thread_id=thread_id, status=Status.IN_REVIEW)


class TestWhatAReviewFound:
    """Cases an adversarial pass over this service turned up, each of which shipped broken."""

    async def test_a_closed_issue_refuses_any_other_status(
        self,
        workflow: ItemWorkflow,
        issue_service: ItemSyncService,
        github: FakeGitHubClient,
        registered: Repository,
        issue_event,
    ) -> None:
        """Closing is what makes an issue DONE, and the sync path re-asserts that on every
        delivery. Writing BACKLOG here put the label on GitHub, reported success, and was then
        overwritten by the command's own re-render, leaving the two permanently disagreeing.
        """
        closed = issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")
        github.issues[ISSUE_KEY] = closed
        result = await issue_service.sync(closed)

        with pytest.raises(WorkflowRefusedError, match="Reopen it there"):
            await workflow.set_status(thread_id=result.thread_id, status=Status.BACKLOG)

        assert github.label_calls == [], "a status that cannot hold was still written to GitHub"

    async def test_a_finished_pull_request_can_be_asked_to_lock_again(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """Locking is the last step and the likeliest to be refused, so it needs a second go.

        Being already DONE used to fail the READY_FOR_MERGE gate, which meant a /set_done whose
        lock failed could never be repaired: the retry was refused for being what the first run
        had made it.
        """
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)
        threads.fail_next_lock = True
        with pytest.raises(DiscordGatewayError):
            await workflow.set_status(thread_id=thread_id, status=Status.DONE)
        assert threads.threads[thread_id].locked is False

        outcome = await workflow.set_status(thread_id=thread_id, status=Status.DONE)

        assert outcome.locked is True
        assert threads.threads[thread_id].locked is True

    async def test_it_locks_the_thread_the_render_actually_wrote_to(
        self, workflow: ItemWorkflow, thread_id: int, threads: FakeThreadGateway
    ) -> None:
        """Somebody deleting the thread mid-command has the sync open a replacement. Locking the
        id the command arrived on would lock a thread that is no longer there."""
        await workflow.set_status(thread_id=thread_id, status=Status.READY_FOR_MERGE)
        await threads.delete(thread_id=thread_id)

        outcome = await workflow.set_status(thread_id=thread_id, status=Status.DONE)

        assert outcome.locked is True
        rebuilt = [t for t in threads.created if t.thread_id not in threads.deleted]
        assert len(rebuilt) == 1
        assert rebuilt[0].locked is True, "the replacement thread was left open"
