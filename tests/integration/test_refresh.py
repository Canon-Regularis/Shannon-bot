"""Mirroring the open backlog nothing ever opened a thread for. Issue #74.

The headline is `test_nobody_is_pinged`. Everything else here is about counting correctly, and a
wrong count is a sentence somebody reads; a refresh that pings is forty people's notifications in
one go, and it only happens once, because the claim on `item_assignments` makes every run after
the first quiet. That is precisely why it cannot be left to be noticed.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _refresh
from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordPermissionError
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.github.errors import GitHubRateLimitError
from shannon.services.sync.items import SyncOutcome, SyncResult, build_item_sync
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.sync.refresh import RefreshScope, RepositoryRefresh
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

REPO = RepositorySnapshot(
    github_repo_id=payloads.REPO_ID,
    owner=payloads.OWNER,
    name=payloads.REPO,
    html_url=f"https://github.com/{payloads.OWNER}/{payloads.REPO}",
)
FULL_NAME = REPO.full_name


def a_pull_request(number: int, **overrides) -> PullRequestSnapshot:
    return replace(
        PullRequestSnapshot(
            repository=REPO,
            github_object_id=700_000 + number,
            number=number,
            title=f"Pull request {number}",
            html_url=f"https://github.com/{FULL_NAME}/pull/{number}",
            state="open",
            author=Actor("octocat"),
            reviewers=(Actor("monalisa"),),
            labels=(Label("backend"),),
        ),
        **overrides,
    )


def an_issue(number: int, **overrides) -> IssueSnapshot:
    return replace(
        IssueSnapshot(
            repository=REPO,
            github_object_id=800_000 + number,
            number=number,
            title=f"Issue {number}",
            html_url=f"https://github.com/{FULL_NAME}/issues/{number}",
            state="open",
            author=Actor("octocat"),
            assignees=(Actor("hubot"),),
        ),
        **overrides,
    )


def github_with(*, pulls=(), issues=()) -> FakeGitHubClient:
    key = FULL_NAME.lower()
    return FakeGitHubClient(
        repositories={key: REPO},
        pull_requests={(key, item.number): item for item in pulls},
        issues={(key, item.number): item for item in issues},
    )


def refresh_with(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
    *,
    cap: int = 25,
    pull_requests=None,
    issues=None,
) -> RepositoryRefresh:
    """The service as the container builds it: both sync services with no notifier, and blocks
    that name people in plain text."""
    return RepositoryRefresh(
        sessionmaker,
        github,
        pull_requests=pull_requests
        or build_item_sync(sessionmaker, threads, PullRequestPolicy(), mentions=False),
        issues=issues or build_item_sync(sessionmaker, threads, IssuePolicy(), mentions=False),
        cap=cap,
    )


async def link_everybody(session: AsyncSession) -> None:
    """Both people the backlog below names, with Discord accounts against them.

    Without this there is no mention for a block to carry, and an assertion that it carries none
    holds on a server where nobody has ever run `/link`, which is not the claim being made.
    """
    store = UserLinkStore(session)
    await store.link(
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
    )
    await store.link(guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=444)
    await session.commit()


def mentions_in_the_blocks(threads: FakeThreadGateway) -> list[str]:
    return [
        threads.metadata_of(thread.thread_id)
        for thread in threads.created
        if "<@" in threads.metadata_of(thread.thread_id)
    ]


class TestMirroringTheBacklog:
    async def test_every_untracked_open_item_gets_a_thread(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12), an_issue(13)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (3, 0, 0)
        assert len(threads.created) == 3
        assert outcome.full_name == FULL_NAME

    async def test_nobody_is_pinged(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The whole reason this service gets its own sync services rather than the ones `/pr`
        holds. A backlog is not news, and the claim on `item_assignments` means a run that pinged
        would be quiet the second time, so this would look fine in every test after the first.

        The block counts as a ping. Opening a thread posts one, a posted message notifies
        everybody it mentions, and forty of them in one go is exactly what this command must not
        do. Looking only at `threads.posts` missed that for as long as it was the only assertion.
        """
        await link_everybody(db_session)
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert threads.posts == [], "a refresh said something in a thread"
        assert mentions_in_the_blocks(threads) == [], "a refresh notified people through a block"
        stamps = await db_session.scalars(select(ItemAssignment.notified_at))
        assert list(stamps) != [], "no assignment rows, so this proves nothing"
        assert all(stamp is None for stamp in stamps), "a refresh spent somebody's one ping"

    async def test_the_wiring_hands_it_services_that_cannot_ping(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The service the container builds, rather than one this file assembled.

        The test above proves the service is silent when it is handed silent sync services. This
        proves it is handed them, which is the half that lives in the wiring and the half a later
        edit can undo without any of the rest of this file noticing.
        """
        await link_everybody(db_session)
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        await _refresh(db_sessionmaker, github, threads).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert len(threads.created) == 2, "it mirrored nothing, so this proves nothing"
        assert threads.posts == [], "the wiring gave /refresh a sync service that pings"
        assert mentions_in_the_blocks(threads) == [], "the wiring gave it one that mentions"
        stamps = await db_session.scalars(select(ItemAssignment.notified_at))
        assert all(stamp is None for stamp in stamps)

    async def test_an_item_that_already_has_a_thread_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12)])
        service = refresh_with(db_sessionmaker, threads, github)
        await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)
        written = list(threads.updates)

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 1, 0)
        assert len(threads.created) == 1
        assert threads.updates == written, "it rewrote a thread it was told to leave alone"

    async def test_a_row_with_no_thread_counts_as_untracked(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The row is committed before the Discord call that gives it a thread, so a refused
        create leaves an item recorded here and invisible in the channel. Reading the row alone
        would call that tracked and leave it that way for ever.
        """
        orphan = an_issue(12)
        db_session.add(
            TrackedItem(
                repository_id=registered.id,
                github_object_id=orphan.github_object_id,
                github_object_type=ObjectType.ISSUE,
                github_object_number=orphan.number,
                github_url=orphan.html_url,
                title=orphan.title,
                github_state="open",
            )
        )
        await db_session.commit()

        outcome = await refresh_with(
            db_sessionmaker, threads, github_with(issues=[orphan])
        ).refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already) == (1, 0)
        assert len(threads.created) == 1

    async def test_a_closed_item_is_never_reached_for(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12, state="closed")])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 0, 0)
        assert threads.created == []

    async def test_a_repository_with_nothing_open_is_not_a_failure(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        outcome = await refresh_with(db_sessionmaker, threads, github_with()).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.already, outcome.left) == (0, 0, 0)


class TestWhatEachScopeReads:
    async def test_issues_only_never_asks_for_pull_requests(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert [kind for kind, _ in github.list_calls] == ["issues"]
        assert outcome.mirrored == 1

    async def test_pull_requests_only_never_asks_for_issues(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.PULL_REQUESTS
        )

        assert [kind for kind, _ in github.list_calls] == ["pulls"]
        assert outcome.mirrored == 1

    async def test_everything_reads_both(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(pulls=[a_pull_request(7)], issues=[an_issue(12)])

        await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert [kind for kind, _ in github.list_calls] == ["pulls", "issues"]


class TestTheCap:
    async def test_it_mirrors_no_more_than_the_cap_and_says_what_is_left(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13), an_issue(14)])

        outcome = await refresh_with(db_sessionmaker, threads, github, cap=2).refresh(
            guild_id=1, scope=RefreshScope.ISSUES
        )

        assert (outcome.mirrored, outcome.left) == (2, 1)
        assert len(threads.created) == 2

    async def test_a_second_run_carries_on_where_the_first_stopped(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13), an_issue(14)])
        service = refresh_with(db_sessionmaker, threads, github, cap=2)
        await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.already, outcome.left) == (1, 2, 0)
        assert len(threads.created) == 3

    async def test_the_count_left_covers_both_kinds(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Both lists are read before either is mirrored, even when the first exhausts the cap.
        Otherwise "how many are still untracked" is answerable only for the kind it got to.
        """
        github = github_with(pulls=[a_pull_request(7), a_pull_request(8)], issues=[an_issue(12)])

        outcome = await refresh_with(db_sessionmaker, threads, github, cap=1).refresh(
            guild_id=1, scope=RefreshScope.EVERYTHING
        )

        assert (outcome.mirrored, outcome.left) == (1, 2)
        assert [kind for kind, _ in github.list_calls] == ["pulls", "issues"]


class TestWhenOneItemFails:
    async def test_the_rest_are_still_mirrored(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        github = github_with(issues=[an_issue(12), an_issue(13)])
        threads.fail_next_create = True

        with caplog.at_level(logging.WARNING):
            outcome = await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (1, 1)
        assert outcome.left == 1, "the one that failed is still untracked"
        assert "could not mirror" in caplog.text

    async def test_a_surprise_does_not_strand_the_command(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Deliberately unlike `/pr`, which lets an unexpected failure out. There it is one item
        and the person sees the tree's answer; here it would abandon every item after it and
        leave the command with no reply at all.
        """

        class _Surprising(FakeThreadGateway):
            """Fails the first item and nothing else. Counted rather than read off `created`,
            which stays empty precisely because the first one failed."""

            attempts = 0

            async def create(self, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("something nobody predicted")
                return await super().create(**kwargs)

        broken = _Surprising()
        github = github_with(issues=[an_issue(12), an_issue(13)])

        with caplog.at_level(logging.ERROR):
            outcome = await refresh_with(db_sessionmaker, broken, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (1, 1)
        assert "an unexpected failure" in caplog.text

    async def test_an_item_a_newer_sync_overtook_is_neither_mirrored_nor_failed(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A webhook can attach a thread between the read and the sync. Nothing went wrong and
        nothing was opened, so it belongs in `left`; the next run finds a thread and skips it.
        """

        class _Overtaken:
            async def sync(self, snapshot, **_):
                return SyncResult(outcome=SyncOutcome.STALE, tracked_item_id=1, thread_id=2)

        github = github_with(issues=[an_issue(12)])
        service = refresh_with(db_sessionmaker, threads, github, issues=_Overtaken())

        outcome = await service.refresh(guild_id=1, scope=RefreshScope.ISSUES)

        assert (outcome.mirrored, outcome.failed, outcome.left) == (0, 0, 1)


class TestWhatStopsTheRun:
    async def test_no_channel_mapped_stops_at_the_first_item(
        self,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Nothing about the item decided that, so every one after it would be refused the same
        way, each opening a session and writing the same warning.

        Issues fall back to the pull request channel, so this is a repository with no mapping at
        all rather than one missing its issue channel, which would have worked.
        """
        db_session.add(
            Repository(
                github_repo_id=REPO.github_repo_id,
                repo_name=FULL_NAME,
                repo_url=REPO.html_url,
                discord_guild_id=1,
            )
        )
        await db_session.commit()
        github = github_with(issues=[an_issue(12), an_issue(13)])

        with pytest.raises(SyncFailedError, match="/set_channel"):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert threads.created == []

    async def test_an_unregistered_server_is_told_to_register(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
    ) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await refresh_with(db_sessionmaker, threads, github_with()).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

    async def test_a_name_that_now_serves_somebody_elses_repository_is_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The hazard `/pr` already guards: a freed name taken by somebody else would have this
        mirror a stranger's whole backlog into the channel."""
        stranger = replace(REPO, github_repo_id=REPO.github_repo_id + 1)
        github = github_with(issues=[an_issue(12)])
        github.repositories[FULL_NAME.lower()] = stranger

        with pytest.raises(RepositoryMismatchError, match="Somebody else has taken it"):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

        assert threads.created == []

    async def test_a_spent_rate_limit_comes_back_untouched(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Waiting is the only thing that helps, and the reply table already says how long."""
        github = github_with(issues=[an_issue(12)])
        github.error = GitHubRateLimitError("spent", retry_after=600)

        with pytest.raises(GitHubRateLimitError):
            await refresh_with(db_sessionmaker, threads, github).refresh(
                guild_id=1, scope=RefreshScope.EVERYTHING
            )

    async def test_a_refused_thread_is_one_item_rather_than_the_run(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A permission is permanent, which everywhere else in this project means the delivery
        is dropped. Here it is still one item: the next may live in a channel this bot can write
        in, and abandoning the run would leave the command with no reply.
        """

        class _Refuses(FakeThreadGateway):
            async def create(self, **kwargs):
                raise DiscordPermissionError("Discord will not let the bot create a thread")

        refusing = _Refuses()
        github = github_with(issues=[an_issue(12)])

        with caplog.at_level(logging.WARNING):
            outcome = await refresh_with(db_sessionmaker, refusing, github).refresh(
                guild_id=1, scope=RefreshScope.ISSUES
            )

        assert (outcome.mirrored, outcome.failed) == (0, 1)
        assert "could not mirror" in caplog.text


class TestTheRoleRowsItWrites:
    async def test_the_people_on_an_item_are_still_recorded(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Silent is not the same as incomplete. The block names everybody, the rows are written,
        and the only thing withheld is the notification.
        """
        github = github_with(pulls=[a_pull_request(7)])

        await refresh_with(db_sessionmaker, threads, github).refresh(
            guild_id=1, scope=RefreshScope.PULL_REQUESTS
        )

        roles = await db_session.scalars(
            select(ItemAssignment.role_type).where(ItemAssignment.github_username == "monalisa")
        )
        assert ActorRole.REVIEWER in set(roles)
