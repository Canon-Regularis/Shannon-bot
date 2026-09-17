"""A push to a pull request says what landed on it, through the endpoint and the worker.

Issue #67. A thread said what the item was and what people said about it, and nothing at all
about the work going into it: the delivery that would have said so was matched and dropped at the
endpoint before a row was ever written.

Driven end to end rather than against the announcer directly, because half of what makes this
work is outside it. `synchronize` has to survive the endpoint's own filter, reach the queue, come
back out of it with a delivery number, and find a thread already open. A test that called
`CommitLine.say` with a hand-built arrival would prove none of that.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from shannon.db.models import Repository
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_commit, format_commits_left, format_force_push
from shannon.domain.models import Actor, CommitRange, CommitRef, CommitStats
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.commit_lines import COMMITS_PER_PUSH, CommitLine
from shannon.services.sync.items import build_item_handler, build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration

MARKS = ("📝", "🔁", "-# ")
PUSH = (payloads.BEFORE_PUSH, payloads.AFTER_PUSH)
NUMBERS = CommitStats(additions=4, deletions=1, changed_files=2)


def ref(sha: str, *, by: str | None = "octocat", message: str = "Do the work", merge: bool = False):
    author = Actor(login=by, github_user_id=1) if by is not None else None
    return CommitRef(sha=sha, message=message, author=author, merge=merge)


def ahead(*commits: CommitRef, total: int | None = None) -> CommitRange:
    return CommitRange(
        status="ahead", commits=commits, total=len(commits) if total is None else total
    )


def stocked(compared: CommitRange, *commits: CommitRef, **absent: None) -> FakeGitHubClient:
    """A GitHub that answers one compare and the numbers for the commits named after it.

    Commits left out of the call have no numbers, which is how a test says GitHub has collected
    one: the announcer reads a missing answer as gone rather than as a fake nobody stocked.
    """
    numbers: dict[str, CommitStats | None] = {commit.sha: NUMBERS for commit in commits}
    numbers.update(absent)
    return FakeGitHubClient(compares={PUSH: compared}, commits=numbers)


def said(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if body.startswith(MARKS)]


async def pushed(client, container, *, delivery: str = "push-1", **overrides) -> None:
    """One push, through the endpoint and out of the queue."""
    await post(client, "pull_request", payloads.push_event(**overrides), delivery=delivery)
    await container.worker.run_once()


async def with_a_thread(client, container) -> None:
    """The pull request opened first, because a line needs a thread to be a line in.

    Everything here would otherwise be the first thing said about the item, which is the block,
    and the block is not what these tests are about.
    """
    await post(client, "pull_request", payloads.pull_request_event("opened"), delivery="pr-1")
    await container.worker.run_once()


class TestWhatAPushSays:
    async def test_a_push_says_what_landed(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """The whole feature in one test, and the gate on the action that lets it run at all."""
        threads = FakeThreadGateway()
        one = ref("a" * 40, message="Add the webhook endpoint\n\nAnswers the check.")
        container = build_stack(db_engine, threads=threads, github=stocked(ahead(one), one))

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) == [
            "📝 **octocat** has committed Add the webhook endpoint\n"
            "> Answers the check.\n"
            "-# With changes: +4, -1, 2 files changed"
        ]

    async def test_every_commit_in_the_push_gets_its_own_message(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """One message each rather than one listing them, so a reader sees the same shape whether
        somebody pushed once or five times."""
        threads = FakeThreadGateway()
        commits = [ref("a" * 40, message="One"), ref("b" * 40, message="Two")]
        container = build_stack(
            db_engine, threads=threads, github=stocked(ahead(*commits), *commits)
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert [line.splitlines()[0] for line in said(threads)] == [
            "📝 **octocat** has committed One",
            "📝 **octocat** has committed Two",
        ]

    async def test_a_commit_with_no_github_account_is_still_announced(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """GitHub answers with no account whenever the committing address is registered to
        nobody. Read strictly, "not authored by the pusher" drops those too, and somebody whose
        git address is not on their profile would lose every commit with nothing said."""
        threads = FakeThreadGateway()
        one = ref("a" * 40, by=None, message="Drop the unused import")
        container = build_stack(db_engine, threads=threads, github=stocked(ahead(one), one))

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads)[0].startswith("📝 **Unknown** has committed Drop the unused import")

    async def test_an_action_that_is_not_a_push_says_nothing(
        self, registered: Repository, db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The same announcer is given to the issue handler as well, where it must do nothing at
        all, and a `labeled` on a pull request must not go looking for commits either.

        The log is asserted on because it is the only thing that tells the gate on the action
        apart from the gate on the range. Without the first, every ordinary delivery falls through
        to the second and complains that it carried no commits, which is a warning on every
        label, comment and close this bot ever handles.
        """
        threads = FakeThreadGateway()
        one = ref("a" * 40)
        github = stocked(ahead(one), one)
        container = build_stack(db_engine, threads=threads, github=github)

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            with caplog.at_level(logging.WARNING):
                await post(
                    client, "pull_request", payloads.pull_request_event("labeled"), delivery="tag-1"
                )
                await container.worker.run_once()

        assert said(threads) == []
        assert github.compare_calls == [], "it went to GitHub about a delivery that was not a push"
        assert caplog.records == [], "an ordinary delivery complained about not being a push"


class TestWhatIsLeftOut:
    async def test_a_merge_of_main_announces_nothing(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """The case that decides whether this feature is usable. Keeping a branch up to date with
        main brings in everybody's commits behind one merge, and announcing them would make a
        busy repository unreadable.

        The second assertion is the one worth having: the filter runs on the compare's own rows,
        before a single read is spent. Counting the lines cannot show that, because a push that
        announces nothing looks identical whether it skipped the reads or made forty and threw
        the answers away.
        """
        threads = FakeThreadGateway()
        merge = ref("a" * 40, message="Merge branch 'main'", merge=True)
        theirs = ref("b" * 40, by="monalisa", message="Somebody else's work")
        github = stocked(ahead(merge, theirs), merge, theirs)
        container = build_stack(db_engine, threads=threads, github=github)

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) == []
        assert github.stats_calls == [], "it paid for numbers it was never going to use"

    async def test_a_commit_by_somebody_else_in_the_same_push_is_left_out(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        threads = FakeThreadGateway()
        mine = ref("a" * 40, message="Mine")
        theirs = ref("b" * 40, by="monalisa", message="Theirs")
        container = build_stack(
            db_engine, threads=threads, github=stocked(ahead(mine, theirs), mine, theirs)
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert [line.splitlines()[0] for line in said(threads) if line.startswith("📝")] == [
            "📝 **octocat** has committed Mine"
        ]

    async def test_the_name_compared_is_the_account_and_not_the_case_it_was_typed_in(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """GitHub logins are case-insensitive and it echoes back whatever case a payload was
        written with, so matching them exactly drops somebody's own commits at random."""
        threads = FakeThreadGateway()
        mine = ref("a" * 40, by="OctoCat", message="Mine")
        container = build_stack(db_engine, threads=threads, github=stocked(ahead(mine), mine))

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) != []

    async def test_over_the_cap_the_newest_are_the_ones_said(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """A branch that has been running a while puts the oldest commits at the front of the
        range, and the ones somebody is waiting to review are at the end."""
        threads = FakeThreadGateway()
        commits = [
            ref(f"{number:040d}", message=f"Commit {number}")
            for number in range(COMMITS_PER_PUSH + 2)
        ]
        container = build_stack(
            db_engine, threads=threads, github=stocked(ahead(*commits), *commits)
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        announced = [line.splitlines()[0] for line in said(threads) if line.startswith("📝")]
        assert len(announced) == COMMITS_PER_PUSH
        assert announced[-1].endswith(f"Commit {COMMITS_PER_PUSH + 1}")
        assert announced[0].endswith("Commit 2"), "it said the oldest instead of the newest"

    async def test_the_ones_over_the_cap_are_counted_at_the_end(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        threads = FakeThreadGateway()
        commits = [
            ref(f"{number:040d}", message=f"Commit {number}")
            for number in range(COMMITS_PER_PUSH + 2)
        ]
        container = build_stack(
            db_engine, threads=threads, github=stocked(ahead(*commits), *commits)
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads)[-1] == "-# 2 earlier commits in this push were not announced."

    async def test_the_commits_github_never_listed_are_counted_too(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """A compare stops at 250 rows while `total_commits` keeps counting, so the difference
        between the two is commits nothing here ever saw. Nothing can be said about who wrote
        them, and they were certainly not announced."""
        threads = FakeThreadGateway()
        one = ref("a" * 40)
        container = build_stack(
            db_engine, threads=threads, github=stocked(ahead(one, total=300), one)
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads)[-1] == "-# 299 earlier commits in this push were not announced."

    async def test_a_push_of_only_its_own_work_says_nothing_about_what_was_left(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        threads = FakeThreadGateway()
        one = ref("a" * 40)
        container = build_stack(db_engine, threads=threads, github=stocked(ahead(one), one))

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert [line for line in said(threads) if "not announced" in line] == []


class TestABranchThatWasRewritten:
    async def test_a_rebase_says_so_once_instead_of_announcing_the_commits(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """Every commit on a rebased branch has a new SHA, so announcing them says five things
        nobody did just now."""
        threads = FakeThreadGateway()
        github = FakeGitHubClient(
            compares={PUSH: CommitRange(status="diverged", commits=(), total=3)}
        )
        container = build_stack(db_engine, threads=threads, github=github)

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) == [
            "🔁 **octocat** force-pushed this branch, so the commits it replaced are not announced."
        ]
        assert github.stats_calls == []

    async def test_a_branch_reset_backwards_is_a_force_push_too(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """`reset --hard HEAD~3 && push --force` leaves nothing ahead at all: GitHub answers
        `behind` with `total_commits` at zero, checked against the live API. Without this the
        thread says nothing whatever about three commits being thrown away."""
        threads = FakeThreadGateway()
        container = build_stack(
            db_engine,
            threads=threads,
            github=FakeGitHubClient(
                compares={PUSH: CommitRange(status="behind", commits=(), total=0)}
            ),
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads)[0].startswith("🔁 **octocat** force-pushed")

    async def test_two_force_pushes_in_a_row_are_both_announced(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """Keyed on the delivery, because a force push is a fact about one. Two rebases an hour
        apart are two separate things that happened, and a constant key would say the second one
        had already been announced."""
        threads = FakeThreadGateway()
        rewritten = CommitRange(status="diverged", commits=(), total=1)
        container = build_stack(
            db_engine,
            threads=threads,
            github=FakeGitHubClient(
                compares={PUSH: rewritten, ("3" * 40, "4" * 40): rewritten},
            ),
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container, delivery="push-1")
            await pushed(client, container, delivery="push-2", before="3" * 40, after="4" * 40)

        assert len(said(threads)) == 2

    async def test_one_delivery_handled_twice_says_it_once(
        self, registered: Repository, db_sessionmaker
    ) -> None:
        """The other half of the same key, and the claim is the only thing holding it up.

        Driven through the handler rather than the endpoint on purpose. A second POST of a
        delivery already processed is answered `duplicate` and never revived, so it would never
        reach the announcer at all and the test would pass with no claim taken whatsoever. What
        does reach it twice is a delivery whose status could not be written, which comes back out
        of the queue under the same number and is handled again from the top.
        """
        threads = FakeThreadGateway()
        github = FakeGitHubClient(
            compares={PUSH: CommitRange(status="diverged", commits=(), total=1)}
        )
        handle = handler_for(db_sessionmaker, threads, github)
        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        push = payloads.push_event()

        await handle("synchronize", push, 900_002)
        await handle("synchronize", push, 900_002)

        assert len(said(threads)) == 1

    async def test_a_push_that_changed_nothing_says_nothing(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """`identical` is what a force push of the same tree looks like. It is neither work to
        announce nor a rewrite worth reporting.

        The guard behind this has no mutation gate and that is said rather than hidden: an
        identical compare carries no commits and a total of zero, so deleting the check changes
        nothing observable today. It stays because the four statuses are GitHub's list and not
        this project's, and a fifth one added later would otherwise be read as work that landed.
        """
        threads = FakeThreadGateway()
        container = build_stack(
            db_engine,
            threads=threads,
            github=FakeGitHubClient(
                compares={PUSH: CommitRange(status="identical", commits=(), total=0)}
            ),
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) == []


class RefusesOnePost(FakeThreadGateway):
    """A Discord that refuses the nth post and works either side of it.

    Its own class here rather than a flag on the shared fake, because nothing else in the suite
    needs to fail partway through a batch: every other announcer posts exactly one message, so
    the case does not exist for them.
    """

    def __init__(self, *, refuse: int) -> None:
        super().__init__()
        self._refuse = refuse
        self._posts = 0

    async def post(self, *, thread_id: int, content: str, notify=None) -> int | None:
        self._posts += 1
        if self._posts == self._refuse:
            raise DiscordGatewayError("Discord would not take that message")
        return await super().post(thread_id=thread_id, content=content, notify=notify)


def handler_for(sessionmaker, threads: FakeThreadGateway, github: FakeGitHubClient):
    """The item handler with a commit line behind it, built the way the container builds one.

    Used by the pair of tests below that hand the SAME delivery number in twice. The endpoint and
    the worker cannot say that: a delivery that failed is put back with a five second wait on it,
    so `run_once` a moment later finds nothing ready and the retry never happens inside the test.
    """
    return build_item_handler(
        build_item_sync(sessionmaker, threads, PullRequestPolicy()),
        parse_pull_request_event,
        announce=CommitLine(
            sessionmaker,
            threads,
            github,
            render=format_commit,
            rewritten=format_force_push,
            left=format_commits_left,
            shut_again=KeepsThreadsShut(sessionmaker, threads),
        ),
    )


def titles(threads: FakeThreadGateway) -> list[str]:
    return [line.splitlines()[0].split("has committed ")[-1] for line in said(threads)]


class TestADeliveryThatDidNotFinish:
    async def test_a_delivery_retried_after_three_of_five_says_only_the_other_two(
        self, registered: Repository, db_sessionmaker
    ) -> None:
        """The whole argument for keying on the SHA rather than on the delivery, in one test.

        A label move is a fact about a delivery, so `LabelLine` keys on one. A commit is a fact
        about a SHA, and one delivery carries up to ten of them. Keyed on the delivery, this run
        would find the key claimed on its retry and post nothing, losing two commits for good
        with the delivery recorded as handled.

        The fourth post rather than the first, because three landing and two not is the shape
        that tells the two keys apart. A failure on the first would look the same either way.
        """
        threads = RefusesOnePost(refuse=4)
        commits = [ref(f"{number:040d}", message=f"Commit {number}") for number in range(5)]
        handle = handler_for(db_sessionmaker, threads, stocked(ahead(*commits), *commits))
        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        push = payloads.push_event()

        with pytest.raises(DiscordGatewayError):
            await handle("synchronize", push, 900_002)
        assert titles(threads) == ["Commit 0", "Commit 1", "Commit 2"]

        await handle("synchronize", push, 900_002)

        assert titles(threads) == [f"Commit {number}" for number in range(5)]

    async def test_the_claim_on_the_one_that_was_refused_is_given_back(
        self, registered: Repository, db_sessionmaker
    ) -> None:
        """The half of the same run that is easy to get wrong. A claim taken and not handed back
        reads on the retry as already announced, so the commit that was never posted is lost for
        good while everything after it carries on."""
        threads = RefusesOnePost(refuse=4)
        commits = [ref(f"{number:040d}", message=f"Commit {number}") for number in range(5)]
        handle = handler_for(db_sessionmaker, threads, stocked(ahead(*commits), *commits))
        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        push = payloads.push_event()
        with pytest.raises(DiscordGatewayError):
            await handle("synchronize", push, 900_002)

        await handle("synchronize", push, 900_002)

        assert "Commit 3" in titles(threads), "the claim was never given back"

    async def test_a_commit_that_vanished_between_the_compare_and_the_read_is_skipped(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """A rebase landing while this runs leaves SHAs the compare listed and the read cannot
        find. A SHA GitHub has collected never comes back, so retrying the delivery spends
        sixteen attempts over two hours to be told the same thing."""
        threads = FakeThreadGateway()
        here = ref("a" * 40, message="Still here")
        gone = ref("b" * 40, message="Gone")
        container = build_stack(
            db_engine,
            threads=threads,
            github=stocked(ahead(here, gone), here, **{"b" * 40: None}),
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert [line.splitlines()[0] for line in said(threads) if line.startswith("📝")] == [
            "📝 **octocat** has committed Still here"
        ]
        assert await client.outcome_of("push-1") == "processed", "the delivery was left to retry"

    async def test_the_one_it_could_not_read_is_counted_as_left_over(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        threads = FakeThreadGateway()
        here = ref("a" * 40, message="Still here")
        gone = ref("b" * 40, message="Gone")
        container = build_stack(
            db_engine,
            threads=threads,
            github=stocked(ahead(here, gone), here, **{"b" * 40: None}),
        )

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads)[-1] == "-# 1 earlier commit in this push was not announced."

    async def test_a_push_whose_compare_github_has_nothing_for_says_nothing(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """A branch deleted between the push and the read. The delivery is done rather than
        retried, because the answer will be the same in two hours."""
        threads = FakeThreadGateway()
        container = build_stack(db_engine, threads=threads, github=FakeGitHubClient())

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert said(threads) == []
        assert await client.outcome_of("push-1") == "processed", "the delivery was left to retry"

    async def test_a_push_with_no_range_on_it_never_reaches_github(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """The two SHAs are the only thing a `synchronize` payload says about what moved. Without
        them there is no question to ask, and asking anyway would compare against nothing."""
        threads = FakeThreadGateway()
        github = FakeGitHubClient()
        container = build_stack(db_engine, threads=threads, github=github)

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container, before="0" * 40)

        assert github.compare_calls == []
        assert said(threads) == []
        assert await client.outcome_of("push-1") == "processed", "the delivery was left to retry"


class TestTheQueueItNowGoesThrough:
    async def test_a_push_is_written_down_rather_than_dropped_at_the_door(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        """The change with the largest cost in the whole issue, said out loud. Every push to
        every open pull request now writes a row carrying the payload, where before the endpoint
        matched the action and dropped it."""
        container = build_stack(db_engine, github=FakeGitHubClient())

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            response = await post(client, "pull_request", payloads.push_event(), delivery="push-1")

        assert response.json()["status"] == "accepted"

    async def test_the_compare_is_asked_for_with_the_two_ends_off_the_payload(
        self, registered: Repository, db_engine: AsyncEngine
    ) -> None:
        github = FakeGitHubClient()
        container = build_stack(db_engine, github=github)

        async with build_http_client(container) as client:
            await with_a_thread(client, container)
            await pushed(client, container)

        assert github.compare_calls == [
            (f"{payloads.OWNER}/{payloads.REPO}".lower(), payloads.BEFORE_PUSH, payloads.AFTER_PUSH)
        ]
