"""CI results reaching a pull request's thread, and ringing the right people. Issue #112.

Driven through the endpoint and the worker rather than against the announcer, because half of
what makes this work is outside it. `check_suite` has to survive the endpoint's own filter, which
it could not before this issue added the key, reach the queue, come back out of it, and find a
thread already open.

The two tests that matter most are the ones about who is rung. A pass goes to the reviewers and a
failure to the author and the assignees, and getting that backwards is silent: everybody still
gets a message, it just reaches the wrong people and the ones who needed it hear nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import Notify
from shannon.domain.enums import ObjectType
from shannon.domain.models import CheckRun
from shannon.github import mapping
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import register_repository
from tests.support.stack import DeliveryClient, build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration

SHA = payloads.CHECKED_SHA
MOVED_ON = "d" * 40
REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()
AUTHOR = "octocat"
ASSIGNEE = "hubot"
REVIEWER = "monalisa"


def run(number: int, name: str, conclusion: str = "success", status: str = "completed") -> CheckRun:
    return CheckRun(
        check_run_id=number,
        name=name,
        status=status,
        conclusion=conclusion,
        html_url=f"https://github.com/o/r/actions/runs/1/job/{number}",
    )


# Seven succeeded and one skipped is this repository's own shape, and the case the three buckets
# exist for: two buckets would call it a failure and ring the author on every green build.
GREEN = [run(1, "Lint"), run(2, "Tests"), run(3, "Publish", "skipped")]
RED = [run(1, "Lint"), run(2, "Tests", "failure")]


def a_github(**overrides: Any) -> FakeGitHubClient:
    """A GitHub holding the pull request the thread is about, and the checks on a commit."""
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    snapshot = mapping.pull_request(payloads.pull_request(**overrides), repo)
    assert snapshot is not None
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): snapshot})
    github.check_runs[SHA] = GREEN
    return github


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[tuple[DeliveryClient, FakeGitHubClient]]:
    """A pull request that already has its thread, and a GitHub that answers about it."""
    await register_repository(db_session, guild_id=1, channel_id=99)
    github = a_github()
    async with build_http_client(build_stack(db_engine, threads=threads, github=github)) as client:
        await deliver(client, "pull_request", payloads.pull_request_event("opened"), delivery="p0")
        yield client, github


def announced(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if "succeeded." in body]


def allow_list(threads: FakeThreadGateway) -> Notify:
    for kind, _, body, notify in threads.allowed:
        if kind == "post" and "succeeded." in body:
            return notify
    return None


async def a_suite(client: DeliveryClient, *, delivery: str = "cs-1", **overrides: Any) -> None:
    await deliver(client, "check_suite", payloads.check_suite_event(**overrides), delivery=delivery)


async def link(session: AsyncSession, login: str, discord_id: int, github_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=github_id, discord_user_id=discord_id
    )
    await session.commit()


class TestWhatReachesTheThread:
    async def test_a_finished_suite_says_what_happened(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        client, _ = tracked

        await a_suite(client)

        assert len(announced(threads)) == 1
        assert "2 / 3 jobs have succeeded." in announced(threads)[0]

    async def test_a_failure_lists_the_broken_job_with_its_log(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        client, github = tracked
        github.check_runs[SHA] = RED

        await a_suite(client)

        said = announced(threads)[0]
        assert "**Unsuccessful Jobs:**" in said
        assert "- `Tests` <https://github.com/o/r/actions/runs/1/job/2>" in said

    async def test_a_skipped_job_is_not_reported_as_broken(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """This repository's `Publish` job is skipped on every pull request."""
        client, _ = tracked

        await a_suite(client)

        assert "Unsuccessful" not in announced(threads)[0]

    async def test_the_delivery_is_reported_as_handled(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient]
    ) -> None:
        client, _ = tracked

        await a_suite(client, delivery="cs-handled")

        assert await client.outcome_of("cs-handled") == "processed"


class TestWhoIsRung:
    async def test_a_pass_rings_the_reviewers(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        client, _ = tracked
        await link(db_session, REVIEWER, 555, 200)

        await a_suite(client)

        assert "<@555>" in announced(threads)[0]
        assert allow_list(threads) == (555,)

    async def test_a_failure_rings_the_author_and_the_assignees_instead(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The reviewers are deliberately absent. A broken build is the author's to fix, and
        ringing the people who have not looked at it yet is the wrong end of the feature."""
        client, github = tracked
        github.check_runs[SHA] = RED
        await link(db_session, AUTHOR, 111, 583231)
        await link(db_session, ASSIGNEE, 222, 100)
        await link(db_session, REVIEWER, 555, 200)

        await a_suite(client)

        said = announced(threads)[0]
        assert "<@111>" in said
        assert "<@222>" in said
        assert "<@555>" not in said, "a reviewer was rung about a build they had not asked to see"
        assert sorted(allow_list(threads) or ()) == [111, 222]

    async def test_a_muted_reviewer_is_named_without_being_rung(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """`/mentions` decides whether your own name notifies you. The thread still records who
        the result was for."""
        client, _ = tracked
        await link(db_session, REVIEWER, 555, 200)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=555)
        await db_session.commit()

        await a_suite(client)

        assert "<@555>" in announced(threads)[0]
        assert allow_list(threads) == ()

    async def test_a_draft_posts_the_results_and_rings_nobody(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """GitHub runs CI on a draft like any other pull request, and nobody has been asked to
        review it yet."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, REVIEWER, 555, 200)
        github = a_github(draft=True)
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert announced(threads), "a draft should still report what CI did"
        assert "<@" not in announced(threads)[0]
        assert allow_list(threads) == ()


class TestWhatItSaysNothingAbout:
    async def test_a_suite_with_a_job_still_running(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """Two apps report on one commit separately, so the first to finish must wait for the
        rest rather than announce a fraction of the answer."""
        client, github = tracked
        github.check_runs[SHA] = [run(1, "Lint"), run(2, "Tests", "", status="in_progress")]

        await a_suite(client)

        assert announced(threads) == []

    async def test_a_suite_for_a_commit_the_branch_has_moved_off(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """A new push cancels the run before it, and that run completes as `cancelled` with its
        finished jobs still reading `success`, which looks like a partial failure."""
        client, github = tracked
        github.check_runs[MOVED_ON] = RED

        await a_suite(client, head_sha=MOVED_ON)

        assert announced(threads) == []

    async def test_a_suite_where_nothing_ran(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """A path filter skipped every job on a docs-only push."""
        client, github = tracked
        github.check_runs[SHA] = [run(1, "Lint", "skipped"), run(2, "Tests", "skipped")]

        await a_suite(client)

        assert announced(threads) == []

    async def test_a_suite_heading_no_pull_request(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """A push to the default branch. The endpoint that would chase it answers with the pull
        request the commit was merged by, which is why nothing chases it."""
        client, github = tracked

        await a_suite(client, numbers=())

        assert announced(threads) == []
        assert github.check_run_calls == [], "GitHub was asked about a suite with nowhere to go"

    async def test_a_pull_request_that_has_closed(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Posting reopens an archived thread, and a merged pull request does not want a late
        CI result either way."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        github = a_github(state="closed", merged=True)
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert announced(threads) == []

    async def test_a_pull_request_this_bot_does_not_track(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The repository is registered but nothing has ever opened a thread for this item, which
        is every pull request that predates the registration."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        github = a_github()
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await a_suite(client)

        assert announced(threads) == []
        assert github.check_run_calls == []

    async def test_a_tracked_pull_request_whose_thread_is_not_built_yet_is_retried(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        registered: Repository,
        threads: FakeThreadGateway,
    ) -> None:
        """Retried rather than dropped. A suite can finish while the delivery that builds the
        thread is still behind a Discord outage, and answering "nothing to do" loses the result
        for good because nothing ever revisits a delivery that said that.
        """
        db_session.add(
            TrackedItem(
                repository_id=registered.id,
                github_object_id=payloads.PR_ID,
                github_object_type=ObjectType.PR,
                github_object_number=7,
                github_url="",
                title="No thread yet",
                discord_thread_id=None,
            )
        )
        await db_session.commit()

        async with build_http_client(
            build_stack(db_engine, threads=threads, github=a_github())
        ) as client:
            await a_suite(client, delivery="cs-retry")
            attempts = await client.attempts_of("cs-retry")
            outcome = await client.outcome_of("cs-retry")

        assert announced(threads) == []
        # Tried once and still owed. `ignored` here would mean the result was thrown away, and
        # nothing ever revisits a delivery that said that; `pending` means it comes back once the
        # backoff is up, by which time the thread exists.
        assert (attempts, outcome) == (1, "pending")

    async def test_github_listing_no_checks_at_all(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """An empty list and a collected commit are different answers and both mean there is
        nothing to report."""
        client, github = tracked
        github.check_runs[SHA] = []

        await a_suite(client)

        assert announced(threads) == []

    async def test_a_commit_github_has_collected(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        client, github = tracked
        github.check_runs[SHA] = None

        await a_suite(client)

        assert announced(threads) == []

    async def test_an_unregistered_repository(
        self, db_engine: AsyncEngine, threads: FakeThreadGateway
    ) -> None:
        github = a_github()
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await a_suite(client)

        assert announced(threads) == []
        assert github.check_run_calls == []


class TestSayingItOnce:
    async def test_the_same_delivery_twice_posts_once(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        client, _ = tracked

        await a_suite(client, delivery="cs-a")
        await a_suite(client, delivery="cs-b")

        assert len(announced(threads)) == 1

    async def test_a_second_provider_finishing_later_posts_again(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """The case the count in the key exists for. Run ids are handed out at CREATION, so a
        provider whose runs were made earlier and finish later leaves the largest id exactly where
        it was, and a key built on that alone would find the claim taken and say nothing."""
        client, github = tracked
        github.check_runs[SHA] = [*GREEN, run(0, "Coverage")]

        await a_suite(client, delivery="cs-a")
        github.check_runs[SHA] = [*GREEN, run(0, "Coverage"), run(-1, "Security")]
        await a_suite(client, delivery="cs-b")

        assert len(announced(threads)) == 2

    async def test_a_re_run_posts_again(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """Re-running rotates the ids, which is what a reader waiting on a flake wants to hear."""
        client, github = tracked
        github.check_runs[SHA] = RED

        await a_suite(client, delivery="cs-a")
        github.check_runs[SHA] = [run(90, "Lint"), run(91, "Tests")]
        await a_suite(client, delivery="cs-b")

        assert len(announced(threads)) == 2
        assert "Unsuccessful" in announced(threads)[0]
        assert "Unsuccessful" not in announced(threads)[1]


class TestTheAllowListTheLineCarries:
    """`ClaimedLine` gained a `notify` for this feature. None and () are different answers and
    both are falsy, so reading one as the other turns "ring nobody" into "ring everybody named"."""

    async def test_a_line_with_nobody_to_ring_says_nobody_rather_than_no_opinion(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """A draft. `()` leaves Discord ringing nobody; `None` would leave the client's own rule in
        force, which permits every user mention in the message."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        github = a_github(draft=True)
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert allow_list(threads) == (), "a draft must say nobody, not say nothing"
        assert allow_list(threads) is not None

    async def test_the_lines_that_never_ring_anybody_still_say_nothing_about_it(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """Every other claimed line passes no allow-list at all, and this feature must not have
        changed that: the tag and state lines build no mentions, so the client's own rule is the
        right one for them."""
        client, _ = tracked
        # The payload helper builds the inner pull request, so the top-level `label` that makes
        # this a label move rather than an ordinary edit is added here, as the tag-line tests do.
        body = payloads.pull_request_event("labeled", labels=[{"name": "bug"}])
        body["label"] = {"name": "bug", "color": "d73a4a"}

        await deliver(client, "pull_request", body, delivery="lab-1")

        tagged = [
            notify
            for kind, _, said, notify in threads.allowed
            if kind == "post" and "bug" in said and "succeeded." not in said
        ]
        assert tagged == [None], "a line that names nobody started carrying an allow-list"
