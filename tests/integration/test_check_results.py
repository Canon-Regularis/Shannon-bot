"""CI results reaching a pull request's thread, and ringing the right people. Issue #112.

Driven through the endpoint and the worker rather than against the announcer, because half of
what makes this work is outside it. `check_suite` has to survive the endpoint's own filter, which
it could not before this issue added the key, reach the queue, come back out of it, and find a
thread already open.

The tests that matter most are the ones about who is rung, and issue #164 is what they say now.
Both outcomes reach the same people: the author, the assignees, and whoever has a commit on the
pull request. The verdict decides what the sentence says, not who hears it.

It used to turn on the verdict. A pass rang the requested reviewers and a failure rang the author
and the assignees, so somebody whose push went green was told nothing while a reviewer who had not
looked yet was interrupted - which is how #164 was reported, with a screenshot of exactly that.
Getting this wrong is silent: everybody still gets a message, it just reaches the wrong people and
the ones who needed it hear nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import Notify
from shannon.domain.enums import ObjectType
from shannon.domain.models import Actor, CheckRun, CommitRef
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
# Somebody who pushed to the branch and is neither its author nor assigned to it. Before issue
# #164 there was no name for this person here, because nothing ever rang them.
CONTRIBUTOR = "defunkt"


def wrote(login: str | None, *, merge: bool = False, sha: str = "a" * 40) -> CommitRef:
    """One commit on the pull request, as the commits endpoint describes it."""
    return CommitRef(
        sha=sha, message="Add the thing", author=Actor(login) if login else None, merge=merge
    )


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


def a_github(commits: Sequence[CommitRef] | None = None, **overrides: Any) -> FakeGitHubClient:
    """A GitHub holding the pull request, the checks on its commit, and who wrote them.

    The commits default to one by `CONTRIBUTOR`, because since issue #164 that is the ordinary
    shape: somebody pushed, the jobs ran, and the result is theirs to hear about. A test that
    wants the other case passes its own list, or `[]` for a pull request nobody can be resolved
    from.
    """
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    snapshot = mapping.pull_request(payloads.pull_request(**overrides), repo)
    assert snapshot is not None
    commits = [wrote(CONTRIBUTOR)] if commits is None else commits
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): snapshot})
    github.check_runs[SHA] = GREEN
    github.pull_request_commits[(REPO_FULL, 7)] = commits
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


def allow_lists(threads: FakeThreadGateway) -> list[Notify]:
    """Every announcement's allow-list, in the order they were posted.

    `allow_list` answers about the first and is right for the tests that post once. A test that
    has to compare two announcements against EACH OTHER needs both, and comparing them is the only
    way to assert that two outcomes reach the same people rather than asserting the same literal
    twice and hoping.
    """
    return [
        notify
        for kind, _, body, notify in threads.allowed
        if kind == "post" and "succeeded." in body
    ]


async def a_suite(
    client: DeliveryClient,
    *,
    delivery: str = "cs-1",
    expect_retries: bool = False,
    **overrides: Any,
) -> None:
    await deliver(
        client,
        "check_suite",
        payloads.check_suite_event(**overrides),
        delivery=delivery,
        expect_retries=expect_retries,
    )


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
    """Issue #164. The audience is everybody who put the code there, on both outcomes.

    It used to turn on the verdict: a pass rang the requested reviewers and a failure rang the
    author and the assignees. So the people who caused a green run were told nothing, and people
    who had not looked at it yet were interrupted - which is what the issue reported, with a
    screenshot of a reviewer being pinged about somebody else's passing build.
    """

    async def test_a_pass_rings_the_people_who_caused_the_run(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The regression test for #164, and the reviewer assertion is the half that matters."""
        client, _ = tracked
        await link(db_session, AUTHOR, 111, 583231)
        await link(db_session, ASSIGNEE, 222, 100)
        await link(db_session, CONTRIBUTOR, 333, 300)
        await link(db_session, REVIEWER, 555, 200)

        await a_suite(client)

        said = announced(threads)[0]
        assert sorted(allow_list(threads) or ()) == [111, 222, 333]
        assert "<@555>" not in said, "#164 again: a pass rang a reviewer who had not looked yet"

    async def test_a_failure_rings_exactly_who_a_pass_rings(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """Both outcomes in ONE test, compared against each other rather than against a literal.

        This used to drive a failure alone and assert `[111, 222, 333]`, under a name and a
        docstring claiming the two outcomes reach the same people. It could not show that: a
        regression in the pass path only - which is precisely the shape of issue #164 - left it
        green, because it never ran a passing suite. Measured, not supposed: restoring the old
        `if report.passed: return reviewers` kept this test passing while seven others went red.

        Two suites rather than two tests, because the claim is about SAMENESS. `note_key` is
        `checks:{count}:{largest id}`, so a different set of runs is a different claim and the
        second announcement is really posted.
        """
        client, github = tracked
        await link(db_session, AUTHOR, 111, 583231)
        await link(db_session, ASSIGNEE, 222, 100)
        await link(db_session, CONTRIBUTOR, 333, 300)
        await link(db_session, REVIEWER, 555, 200)

        await a_suite(client, delivery="cs-green")
        github.check_runs[SHA] = RED
        await a_suite(client, delivery="cs-red")

        passed, failed = allow_lists(threads)
        assert sorted(passed or ()) == [111, 222, 333], "the pass rang the wrong people"
        assert sorted(failed or ()) == sorted(passed or ()), (
            "the two outcomes reach different people, which is the whole of issue #164"
        )
        assert all("<@555>" not in said for said in announced(threads))

    async def test_a_contributor_who_is_neither_author_nor_assignee_is_rung(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The person the old audience could not reach at all. Nothing on the pull request names
        them - they are only on it because they pushed."""
        client, _ = tracked
        await link(db_session, CONTRIBUTOR, 333, 300)

        await a_suite(client)

        assert "<@333>" in announced(threads)[0]
        assert allow_list(threads) == (333,)

    async def test_the_person_whose_push_broke_it_is_rung_about_their_own_build(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """A deliberate exception to this bot's one self-notification rule.

        `draft_lines` drops whoever pressed the button, because they know - they pressed it. A
        suite finishing is the opposite kind of event: it is news, and it is most news to the
        person who pushed, who cannot know the answer until the jobs come back. Anybody who would
        rather not hear it has `/mentions off`, which is where that choice belongs.

        Green as well as red. "Your build is fixed" is as much news as "your build broke", and a
        test that only ever broke the build could not see a pass-path regression.
        """
        client, github = tracked
        await link(db_session, CONTRIBUTOR, 333, 300)

        await a_suite(client, delivery="cs-green")
        github.check_runs[SHA] = RED
        await a_suite(client, delivery="cs-red")

        # Both outcomes, because the exception is not about bad news. Driving only the failure
        # left this blind to a regression in the pass path, which is the half #164 was about.
        assert [sorted(notify or ()) for notify in allow_lists(threads)] == [[333], [333]], (
            "the person whose push caused the run was not told about it"
        )

    async def test_a_reviewer_is_not_rung_by_ci_at_all(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """Stated on its own, because it is a decision rather than a side effect: a reviewer finds
        out a pull request is ready by being asked for a review, not by CI finishing."""
        client, _ = tracked
        await link(db_session, REVIEWER, 555, 200)

        await a_suite(client)

        assert "<@555>" not in announced(threads)[0]
        assert allow_list(threads) == ()

    async def test_a_commit_github_cannot_link_to_an_account_rings_nobody_extra(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Somebody committed under an address no GitHub account holds. There is nobody to ring,
        and the author is still told."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, AUTHOR, 111, 583231)
        github = a_github(commits=[wrote(None)])
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert allow_list(threads) == (111,)

    async def test_a_contributor_who_is_also_the_author_is_rung_once(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """The ordinary case: somebody opened the pull request and pushed to it. Capitalised
        differently because GitHub echoes a login the way it was typed in each place it appears."""
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, AUTHOR, 111, 583231)
        github = a_github(commits=[wrote("OctoCat")])
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        said = announced(threads)[0]
        assert allow_list(threads) == (111,)
        assert said.count("<@111>") == 1, "the author was named twice for one event"

    async def test_a_merge_commit_does_not_ring_whoever_pressed_update_branch(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Issue #164 arriving through a side door.

        A merge commit's account is whoever pressed "Update branch", which is very often a
        reviewer tidying somebody else's pull request. The commit never leaves the branch, so
        without the filter that reviewer is rung on every suite for the rest of its life - the
        same wrong person the issue is about, by a different route.
        """
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, AUTHOR, 111, 583231)
        await link(db_session, REVIEWER, 555, 200)
        github = a_github(commits=[wrote(REVIEWER, merge=True)])
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert allow_list(threads) == (111,)
        assert "<@555>" not in announced(threads)[0]

    async def test_a_muted_contributor_is_named_without_being_rung(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """`/mentions` decides whether your own name notifies you. The thread still records who
        the result was for - including, now, somebody who only appears on it through a commit."""
        client, _ = tracked
        await link(db_session, CONTRIBUTOR, 333, 300)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=333)
        await db_session.commit()

        await a_suite(client)

        assert "<@333>" in announced(threads)[0]
        assert allow_list(threads) == ()

    async def test_a_draft_posts_the_results_and_rings_nobody(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """A draft is the state in which this bot asks nothing of anybody - the same reason its
        card is grey and the approval round-up refuses one. Somebody iterating on a draft pushes
        repeatedly, and a ping on every red run in that loop is the cost. The panel is still
        posted, so the result is in the thread either way.

        It used to be justified by reviewers not having been asked yet. Reviewers are no longer
        rung by CI at all, so that argument went with them; the rule survives on this one.
        """
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, AUTHOR, 111, 583231)
        await link(db_session, CONTRIBUTOR, 333, 300)
        # Linked on purpose, so a reviewer who reached the audience would arrive as a live mention
        # rather than as plain text. Without this the old assertion passed while the panel read
        # "monalisa Everything that ran passed." - nobody rung, but a name said on a draft, which
        # is not what "rings nobody" claims.
        await link(db_session, REVIEWER, 555, 200)
        github = a_github(draft=True)
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        said = announced(threads)[0]
        assert said, "a draft should still report what CI did"
        assert "<@" not in said, "a draft named somebody as a mention"
        assert not any(login in said for login in (AUTHOR, ASSIGNEE, CONTRIBUTOR, REVIEWER)), (
            f"a draft named somebody in plain text: {said!r}"
        )
        assert allow_list(threads) == ()
        assert github.commit_list_calls == [], "a draft paid for a read it had no use for"


class TestWhenGitHubWillNotSayWhoPushed:
    """The third read can fail, and the message is worth more than the extra names.

    The split is between a 404 and everything else, and the claim is what decides it: `say_once`
    takes a claim under the report's key, so a post with a narrowed audience is PERMANENT for that
    set of runs - those contributors are never rung about it. A 404 means there is nothing to read
    and no later attempt would find any, so narrowing is the whole answer. Anything else is a blip,
    and being late is cheaper than being quietly incomplete.
    """

    async def test_a_commit_list_github_has_lost_still_rings_the_author(
        self,
        db_engine: AsyncEngine,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        await register_repository(db_session, guild_id=1, channel_id=99)
        await link(db_session, AUTHOR, 111, 583231)
        github = a_github()
        github.pull_request_commits.pop((REPO_FULL, 7))
        async with build_http_client(
            build_stack(db_engine, threads=threads, github=github)
        ) as client:
            await deliver(
                client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
            )
            await a_suite(client)

        assert announced(threads), "the result was lost along with the names"
        assert allow_list(threads) == (111,)


class TestWhatItSaysNothingAbout:
    async def test_a_suite_with_a_job_still_running(
        self, tracked: tuple[DeliveryClient, FakeGitHubClient], threads: FakeThreadGateway
    ) -> None:
        """Two apps report on one commit separately, so the first to finish must wait for the
        rest rather than announce a fraction of the answer.

        The second assertion pins an ORDER rather than an outcome. A repository running two checks
        apps refuses most of its suites right here, so the audience is worked out after this and
        not before: the commonest delivery on this path must not pay for a GitHub read it has no
        use for, nor be able to fail on one.
        """
        client, github = tracked
        github.check_runs[SHA] = [run(1, "Lint"), run(2, "Tests", "", status="in_progress")]

        await a_suite(client)

        assert announced(threads) == []
        assert github.commit_list_calls == [], "a suite that said nothing still read the commits"

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
        request the commit was merged by, which is why nothing chases it.

        Dropped at the route, before the body is a row, which is the assertion below that the
        other two cannot make: the parser refuses the same suites, so with the route's own filter
        taken out nothing is announced and nothing is asked of GitHub either and both of those
        stay green. What the filter buys is the 25kB of JSONB a protected default branch would
        otherwise write per push, for a retention window, having never been actionable.
        """
        client, github = tracked

        await a_suite(client, numbers=())

        assert await client.outcome_of("cs-1") == "not queued", (
            "a suite with nowhere to go was written down anyway"
        )
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
            # The one call here that means to leave a delivery parked: the whole point is that
            # it comes back once the backoff is up, so `drain` must not pull it forward and try
            # again, and must not fail for finding it unfinished.
            await a_suite(client, delivery="cs-retry", expect_retries=True)
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
