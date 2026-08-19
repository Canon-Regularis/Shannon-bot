from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError, UnparseableLinkError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositoryRef,
    RepositorySnapshot,
)
from shannon.github.errors import GitHubNotFoundError
from shannon.github.urls import parse_pull_request_url
from shannon.services.sync.items import ItemSyncService, SyncOutcome, SyncResult
from shannon.services.sync.manual import (
    ManualSync,
    SyncFailedError,
    build_issue_sync,
    build_pull_request_sync,
)
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

LINK = "https://github.com/Canon-Regularis/Shannon-bot/pull/7"
REPO = RepositorySnapshot(
    github_repo_id=payloads.REPO_ID,
    owner=payloads.OWNER,
    name=payloads.REPO,
    html_url=f"https://github.com/{payloads.OWNER}/{payloads.REPO}",
)
SNAPSHOT = PullRequestSnapshot(
    repository=REPO,
    github_object_id=payloads.PR_ID,
    number=7,
    title="Add the webhook endpoint",
    html_url=LINK,
    state="open",
    author=Actor("octocat"),
    assignees=(Actor("hubot"),),
    reviewers=(Actor("monalisa"),),
    labels=(Label("backend"),),
)


ISSUE_LINK = "https://github.com/Canon-Regularis/Shannon-bot/issues/12"
ISSUE = IssueSnapshot(
    repository=REPO,
    github_object_id=555001,
    number=12,
    title="The thread stays open after closing",
    html_url=ISSUE_LINK,
    state="open",
    author=Actor("monalisa"),
    assignees=(Actor("hubot"),),
    labels=(Label("bug"),),
)


@pytest.fixture
def github() -> FakeGitHubClient:
    return FakeGitHubClient(
        pull_requests={("canon-regularis/shannon-bot", 7): SNAPSHOT},
        repositories={"canon-regularis/shannon-bot": REPO},
    )


@pytest.fixture
def manual(
    db_sessionmaker: async_sessionmaker,
    github: FakeGitHubClient,
    sync_service: ItemSyncService,
) -> ManualSync:
    return build_pull_request_sync(db_sessionmaker, github, sync_service)


async def test_a_valid_link_opens_a_thread(
    registered: Repository, manual: ManualSync, threads: FakeThreadGateway
) -> None:
    outcome = await manual.sync_link(guild_id=1, link=LINK)

    assert outcome.created is True
    assert outcome.number == 7
    assert outcome.full_name == "Canon-Regularis/Shannon-bot"
    assert len(threads.created) == 1


async def test_running_it_twice_updates_rather_than_duplicates(
    registered: Repository,
    manual: ManualSync,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
) -> None:
    first = await manual.sync_link(guild_id=1, link=LINK)
    second = await manual.sync_link(guild_id=1, link=LINK)

    assert second.created is False
    assert second.thread_id == first.thread_id
    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_it_picks_up_a_pull_request_a_webhook_already_synced(
    registered: Repository,
    manual: ManualSync,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    """Manual and automatic sync share one service, so they share one thread."""
    from_webhook = await sync_service.sync(pr_event("opened"))
    outcome = await manual.sync_link(guild_id=1, link=LINK)

    assert from_webhook is not None
    assert outcome.thread_id == from_webhook.thread_id
    assert outcome.created is False
    assert len(threads.created) == 1


async def test_an_unregistered_guild_is_told_to_register(
    registered: Repository, manual: ManualSync
) -> None:
    with pytest.raises(NotRegisteredError, match="Run /register first"):
        await manual.sync_link(guild_id=2, link=LINK)


async def test_a_link_to_another_repository_is_refused(
    registered: Repository, manual: ManualSync, github: FakeGitHubClient
) -> None:
    with pytest.raises(RepositoryMismatchError, match="not someone/else"):
        await manual.sync_link(guild_id=1, link="https://github.com/someone/else/pull/1")

    assert github.pull_request_calls == []


async def test_an_issue_link_is_refused_before_anything_else(
    registered: Repository, manual: ManualSync, github: FakeGitHubClient
) -> None:
    with pytest.raises(UnparseableLinkError, match="is an issue link"):
        await manual.sync_link(
            guild_id=1, link="https://github.com/Canon-Regularis/Shannon-bot/issues/7"
        )

    assert github.pull_request_calls == []


async def test_a_pull_request_github_does_not_have_is_reported(
    registered: Repository, manual: ManualSync
) -> None:
    with pytest.raises(GitHubNotFoundError):
        await manual.sync_link(
            guild_id=1, link="https://github.com/Canon-Regularis/Shannon-bot/pull/999"
        )


async def test_the_repository_name_match_ignores_case(
    registered: Repository, manual: ManualSync
) -> None:
    outcome = await manual.sync_link(
        guild_id=1, link="https://github.com/canon-regularis/shannon-bot/pull/7"
    )

    assert outcome.created is True


class TestWhenACollaboratorBreaksItsWord:
    """The three refusals no link and no payload can produce.

    Nothing in the wiring reaches them: the link parsers guarantee a number, and /register
    writes the pull request channel mapping in the same transaction as the repository row. They
    are here because the parser and the sync service are both injected, `SyncsItems` is a
    protocol anything can satisfy, and a command that raised nothing would leave the person who
    ran it looking at a spinner. Driven at the seam they guard rather than through the wiring
    that cannot reach them.
    """

    def _manual(self, db_sessionmaker, github, sync, **overrides) -> ManualSync:
        settings = {
            "parse_link": parse_pull_request_url,
            "fetch": lambda owner, name, number: github.get_pull_request(owner, name, number),
            "noun": "pull request",
        }
        return ManualSync(db_sessionmaker, github, sync, **(settings | overrides))

    async def test_a_parser_that_drops_the_number_is_refused_before_github(
        self, registered: Repository, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient
    ) -> None:
        manual = self._manual(
            db_sessionmaker,
            github,
            _Syncs(),
            parse_link=lambda link: RepositoryRef(owner=payloads.OWNER, name=payloads.REPO),
        )

        with pytest.raises(UnparseableLinkError, match="has no pull request number"):
            await manual.sync_link(guild_id=1, link=LINK)

        assert github.pull_request_calls == [], "GitHub was asked for an item with no number"

    async def test_nothing_to_sync_into_is_reported_as_a_missing_channel(
        self, registered: Repository, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient
    ) -> None:
        manual = self._manual(
            db_sessionmaker, github, _Syncs(SyncResult(outcome=SyncOutcome.NOT_TRACKED))
        )

        with pytest.raises(SyncFailedError, match="Run /set_channel first"):
            await manual.sync_link(guild_id=1, link=LINK)

    async def test_a_sync_that_reports_no_thread_is_not_reported_as_success(
        self, registered: Repository, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient
    ) -> None:
        """`ManualSyncOutcome.thread_id` becomes a channel link in the reply, so None here is a
        message pointing at nothing."""
        manual = self._manual(
            db_sessionmaker,
            github,
            _Syncs(SyncResult(outcome=SyncOutcome.SYNCED, tracked_item_id=1, thread_id=None)),
        )

        with pytest.raises(SyncFailedError, match="could not be mirrored"):
            await manual.sync_link(guild_id=1, link=LINK)


class _Syncs:
    """A SyncsItems that answers whatever it was built with."""

    def __init__(self, result: SyncResult | None = None) -> None:
        self.result = result or SyncResult(
            outcome=SyncOutcome.SYNCED, tracked_item_id=1, thread_id=1001
        )

    async def sync(self, snapshot: object) -> SyncResult:
        return self.result


class TestARenamedRepository:
    """GitHub keeps a repository's id across a rename; the stored name catches up on a webhook.

    Until it does, checking the link by name refuses the correct link for a repository that has
    only moved, and no server admin can do anything about it.
    """

    @pytest.fixture
    def renamed(self) -> FakeGitHubClient:
        """The same repository, same id, under the name GitHub uses now."""
        moved = replace(REPO, name="Shannon", html_url="https://github.com/Canon-Regularis/Shannon")
        return FakeGitHubClient(
            pull_requests={("canon-regularis/shannon", 7): replace(SNAPSHOT, repository=moved)},
            repositories={"canon-regularis/shannon": moved},
        )

    @pytest.fixture
    def moved_link(self) -> str:
        return "https://github.com/Canon-Regularis/Shannon/pull/7"

    async def test_a_link_under_the_new_name_is_accepted(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        sync_service: ItemSyncService,
        renamed: FakeGitHubClient,
        moved_link: str,
    ) -> None:
        service = build_pull_request_sync(db_sessionmaker, renamed, sync_service)

        outcome = await service.sync_link(guild_id=1, link=moved_link)

        assert outcome.thread_id is not None
        assert outcome.full_name == "Canon-Regularis/Shannon"

    async def test_the_stored_name_is_brought_up_to_date(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        sync_service: ItemSyncService,
        renamed: FakeGitHubClient,
        moved_link: str,
    ) -> None:
        service = build_pull_request_sync(db_sessionmaker, renamed, sync_service)
        repository_id = registered.id

        await service.sync_link(guild_id=1, link=moved_link)

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None
        assert stored.repo_name == "Canon-Regularis/Shannon"

    async def test_the_matching_link_costs_no_extra_call(
        self, registered: Repository, manual: ManualSync, github: FakeGitHubClient
    ) -> None:
        """The id check only runs on the path that was about to refuse."""
        await manual.sync_link(guild_id=1, link=LINK)

        assert github.repository_calls == []

    async def test_a_repository_unregistered_while_github_was_answering(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        renamed: FakeGitHubClient,
        moved_link: str,
    ) -> None:
        """The rename check reloads the row after the GitHub call, and it may be gone by then.

        The call sits between two transactions on purpose, because holding one open across the
        network is what the sync path refuses to do. That leaves a window, and unregistering is
        the one thing that closes it. Reproduced by deleting the row from inside the fake's
        `get_repository`, which is where the window is: nothing is contrived about the ordering,
        only about who does the deleting.

        There is nothing left to rename and nothing to raise about. The link was confirmed to be
        the same repository by id, so the sync goes ahead on what the payload says.
        """
        original = renamed.get_repository

        async def unregister_mid_call(owner: str, name: str):
            answer = await original(owner, name)
            async with db_sessionmaker() as session, session.begin():
                await session.execute(delete(Repository))
            return answer

        renamed.get_repository = unregister_mid_call
        manual = build_pull_request_sync(db_sessionmaker, renamed, _Syncs())

        outcome = await manual.sync_link(guild_id=1, link=moved_link)

        assert outcome.full_name == "Canon-Regularis/Shannon"
        db_session.expire_all()
        assert (await db_session.scalars(select(Repository))).all() == []

    async def test_a_link_to_a_genuinely_different_repository_is_still_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        sync_service: ItemSyncService,
    ) -> None:
        elsewhere = replace(REPO, github_repo_id=999999, owner="someone", name="else")
        github = FakeGitHubClient(repositories={"someone/else": elsewhere})
        service = build_pull_request_sync(db_sessionmaker, github, sync_service)

        with pytest.raises(RepositoryMismatchError, match="not someone/else"):
            await service.sync_link(guild_id=1, link="https://github.com/someone/else/pull/7")

    async def test_a_link_to_nothing_at_all_is_still_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        sync_service: ItemSyncService,
    ) -> None:
        service = build_pull_request_sync(db_sessionmaker, FakeGitHubClient(), sync_service)

        with pytest.raises(RepositoryMismatchError):
            await service.sync_link(guild_id=1, link="https://github.com/nobody/nothing/pull/7")


class TestSyncingAnIssueByLink:
    """The /issue service path, which no test had ever constructed.

    FakeGitHubClient had no get_issue, so nothing could drive build_issue_sync at all, and the
    Protocol being structural meant nothing complained. /pr was covered throughout.
    """

    @pytest.fixture
    def issue_github(self) -> FakeGitHubClient:
        return FakeGitHubClient(
            issues={("canon-regularis/shannon-bot", 12): ISSUE},
            repositories={"canon-regularis/shannon-bot": REPO},
        )

    @pytest.fixture
    def issues(
        self,
        db_sessionmaker: async_sessionmaker,
        issue_github: FakeGitHubClient,
        issue_service: ItemSyncService,
    ) -> ManualSync:
        return build_issue_sync(db_sessionmaker, issue_github, issue_service)

    async def test_a_valid_link_opens_a_thread(
        self, registered: Repository, issues: ManualSync, threads: FakeThreadGateway
    ) -> None:
        outcome = await issues.sync_link(guild_id=1, link=ISSUE_LINK)

        assert outcome.created is True
        assert outcome.number == 12
        assert len(threads.created) == 1

    async def test_it_records_the_item_as_an_issue(
        self, registered: Repository, issues: ManualSync, db_session: AsyncSession
    ) -> None:
        await issues.sync_link(guild_id=1, link=ISSUE_LINK)

        item = await db_session.scalar(select(TrackedItem))
        assert item is not None
        assert item.github_object_type is ObjectType.ISSUE
        assert item.github_object_number == 12

    async def test_a_second_run_updates_rather_than_opening_another(
        self, registered: Repository, issues: ManualSync, threads: FakeThreadGateway
    ) -> None:
        first = await issues.sync_link(guild_id=1, link=ISSUE_LINK)

        second = await issues.sync_link(guild_id=1, link=ISSUE_LINK)

        assert second.created is False
        assert second.thread_id == first.thread_id
        assert len(threads.created) == 1

    async def test_it_asks_github_for_an_issue_not_a_pull_request(
        self, registered: Repository, issues: ManualSync, issue_github: FakeGitHubClient
    ) -> None:
        await issues.sync_link(guild_id=1, link=ISSUE_LINK)

        assert issue_github.issue_calls == [("canon-regularis/shannon-bot", 12)]
        assert issue_github.pull_request_calls == []

    async def test_a_pull_request_link_is_refused(
        self, registered: Repository, issues: ManualSync
    ) -> None:
        """The parsers keep the two apart before GitHub is asked anything."""
        with pytest.raises(UnparseableLinkError, match="pull request link"):
            await issues.sync_link(guild_id=1, link=LINK)

    async def test_a_missing_issue_is_reported(
        self, registered: Repository, issues: ManualSync
    ) -> None:
        with pytest.raises(GitHubNotFoundError):
            await issues.sync_link(
                guild_id=1, link="https://github.com/Canon-Regularis/Shannon-bot/issues/999"
            )

    async def test_an_unregistered_guild_is_told_to_register(self, issues: ManualSync) -> None:
        with pytest.raises(NotRegisteredError):
            await issues.sync_link(guild_id=1, link=ISSUE_LINK)
