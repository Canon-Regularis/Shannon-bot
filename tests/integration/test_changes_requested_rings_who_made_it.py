"""A review asking for changes rings whoever made the code it is about. Issue #263.

A review asking for changes was posted into its thread and rang nobody but the names its body
typed, so it read like any other comment and nobody who had to act on it was told. It is read
from GitHub now, between finding where the review goes and resolving who it may ring: the files
its inline comments are on and who made each of them, or, when that cannot be told, the pull
request's author and everybody who committed to it in one line.

Driven through the stack the container builds, because the read is wiring: which mirror carries
it, where it sits against the claim, and that only a review asking for changes pays for it. Through
the real worker where what matters is what a failure does to the delivery. Every arm of the read
itself is in the unit tests; these are the paths through the mirror around it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.models import (
    Actor,
    CommitRef,
    PullRequestSnapshot,
    ReviewCommentSnapshot,
)
from shannon.domain.tracking import TrackedEvent
from shannon.github.errors import GitHubRefusedError, GitHubUnavailableError
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.github.webhooks.review_comments import parse_review_comment_event
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import track
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()

# The GitHub ids the payloads and the commits carry, and the Discord accounts each is linked to.
# monalisa reviews and octocat opened the pull request, both as the payload helpers have it.
ACCOUNTS = {"monalisa": 200, "octocat": 583231, "alice": 900, "bob": 901, "dave": 902}
DISCORD = {"monalisa": 606, "octocat": 707, "alice": 808, "bob": 909, "dave": 1010}

NOTES = "shannon/services/notes.py"
README = "README.md"
SETUP = "setup.py"

# The commit the review was made on, which the review payload carries, and the pull request's
# others. One commit on the branch it came from as well, which touched a commented file and is
# nobody's work on this pull request.
REVIEWED = payloads.review()["commit_id"]
FIRST = "a" * 40
SECOND = "b" * 40
UNREVIEWED = "d" * 40
ON_MAIN = "e" * 40


def person(login: str) -> Actor:
    return Actor(login, ACCOUNTS[login])


def commit(sha: str, login: str) -> CommitRef:
    return CommitRef(sha=sha, message=f"work by {login}", author=person(login), merge=False)


def the_pull_request() -> PullRequestSnapshot:
    """The pull request as GitHub would answer for it, for the round-up an approval asks for,
    parsed from the payload the webhook carries so the two cannot describe different things."""
    snapshot = parse_pull_request_event("opened", payloads.pull_request_event("opened"))
    assert isinstance(snapshot, PullRequestSnapshot)
    return snapshot


@pytest.fixture
def github() -> FakeGitHubClient:
    """GitHub as a review asking for changes finds it.

    Four commits: octocat's on notes.py, alice's on notes.py and README.md, dave's on setup.py,
    which nobody comments on, and bob's on README.md, the one reviewed. Each file's history read
    back from the reviewed commit, with a commit from main on notes.py that the pull request never
    carried.
    """
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): the_pull_request()})
    github.pull_request_commits[(REPO_FULL, 7)] = [
        commit(FIRST, "octocat"),
        commit(SECOND, "alice"),
        commit(UNREVIEWED, "dave"),
        commit(REVIEWED, "bob"),
    ]
    github.file_history[(REPO_FULL, REVIEWED, NOTES)] = [
        commit(SECOND, "alice"),
        commit(FIRST, "octocat"),
        commit(ON_MAIN, "dave"),
    ]
    github.file_history[(REPO_FULL, REVIEWED, README)] = [
        commit(REVIEWED, "bob"),
        commit(SECOND, "alice"),
    ]
    github.file_history[(REPO_FULL, REVIEWED, SETUP)] = [commit(UNREVIEWED, "dave")]
    return github


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
) -> AsyncIterator[DeliveryClient]:
    """The whole stack as the container wires it, over a pull request that has its thread, with
    everybody linked."""
    links = UserLinkStore(db_session)
    for login, discord_user_id in DISCORD.items():
        await links.link(
            guild_id=1,
            github_username=login,
            github_user_id=ACCOUNTS[login],
            discord_user_id=discord_user_id,
        )
    await db_session.commit()
    async with registered_stack(db_engine, db_session, threads, github=github) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


def inline(comment_id: int, path: str, *, review_id: int = payloads.REVIEW_ID) -> dict[str, Any]:
    """One inline comment as GitHub delivers it, left in the review `review_id`."""
    return payloads.pull_request_review_comment_event(
        id=comment_id, path=path, pull_request_review_id=review_id
    )


def stock(github: FakeGitHubClient, *comments: dict[str, Any]) -> None:
    """What GitHub lists for the pull request, read through the parser the webhook goes through,
    so a comment GitHub lists and one it delivers cannot describe different things."""
    listed: list[ReviewCommentSnapshot] = []
    for payload in comments:
        snapshot = parse_review_comment_event("created", payload)
        assert isinstance(snapshot, ReviewCommentSnapshot)
        listed.append(snapshot)
    github.review_comments[(REPO_FULL, 7)] = listed


def on_two_files(github: FakeGitHubClient, *, review_id: int = payloads.REVIEW_ID) -> None:
    """monalisa's review, with a comment on notes.py and one on README.md."""
    stock(
        github,
        inline(payloads.REVIEW_COMMENT_ID, NOTES, review_id=review_id),
        inline(payloads.REVIEW_COMMENT_ID + 1, README, review_id=review_id),
    )


def asking(action: str = "submitted", **overrides: Any) -> dict[str, Any]:
    """monalisa asking for changes. Dated in August by the payload helper, so long past the wait
    for a GitHub that is down."""
    fields: dict[str, Any] = {"state": "changes_requested", "body": "Two things, inline."}
    fields.update(overrides)
    return payloads.pull_request_review_event(action, **fields)


def young(**overrides: Any) -> dict[str, Any]:
    """The same review, submitted a moment ago."""
    return asking(submitted_at=datetime.now(UTC).isoformat(), **overrides)


def last_post(threads: FakeThreadGateway) -> tuple[str, object]:
    """The message the last delivery posted, refusing to read anything else as one."""
    kind, _, content, notify = threads.allowed[-1]
    assert kind == "post", f"the last write was a {kind}, so nothing was posted"
    return content, notify


def rung(*logins: str) -> tuple[int, ...]:
    """The Discord accounts a message may ring, in the order the mute check answers them."""
    return tuple(sorted(DISCORD[login] for login in logins))


def reads(github: FakeGitHubClient) -> int:
    """Every read a review asking for changes can make."""
    return (
        len(github.commit_list_calls)
        + len(github.review_comment_calls)
        + len(github.file_history_calls)
    )


class TestOnTheFiles:
    async def test_only_whoever_made_the_commented_files_is_rung(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """dave committed to the pull request and to main, but not to a file the review is on, so
        he is not asked. The reviewer is named and not rung: they know what they asked."""
        on_two_files(github)

        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        content, notify = last_post(threads)
        assert content.startswith("**<@606>** requested changes on the files <t:")
        assert f"`{NOTES}` made by authors <@707>, <@808>" in content
        assert f"`{README}` made by authors <@808>, <@909>" in content
        assert "<@1010>" not in content
        assert "Two things, inline." in content
        assert notify == rung("octocat", "alice", "bob")

    async def test_each_file_is_read_once_at_the_reviewed_commit(
        self, tracked: DeliveryClient, github: FakeGitHubClient
    ) -> None:
        on_two_files(github)

        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        assert github.file_history_calls == [
            (REPO_FULL, REVIEWED, NOTES),
            (REPO_FULL, REVIEWED, README),
        ]

    async def test_a_muted_author_is_named_and_not_rung(
        self,
        tracked: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=DISCORD["alice"])
        await db_session.commit()
        on_two_files(github)

        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        content, notify = last_post(threads)
        assert "<@808>" in content
        assert notify == rung("octocat", "bob")

    async def test_a_reviewer_who_made_a_file_is_named_on_it_and_not_rung(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """The line says who made the file, and monalisa did; the mirror still never rings a
        reviewer for their own review."""
        github.pull_request_commits[(REPO_FULL, 7)] = [
            commit(FIRST, "octocat"),
            commit(REVIEWED, "monalisa"),
        ]
        github.file_history[(REPO_FULL, REVIEWED, README)] = [commit(REVIEWED, "monalisa")]
        on_two_files(github)

        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        content, notify = last_post(threads)
        assert f"`{README}` made by author <@606>" in content
        assert notify == rung("octocat")

    async def test_another_review_asking_for_changes_rings_them_again(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """Every request for changes is somebody asking again, so every one rings."""
        on_two_files(github)
        await deliver(tracked, "pull_request_review", asking(), delivery="r1")
        on_two_files(github, review_id=payloads.REVIEW_ID + 1)

        await deliver(
            tracked,
            "pull_request_review",
            asking(id=payloads.REVIEW_ID + 1, body="Still two things."),
            delivery="r2",
        )

        content, notify = last_post(threads)
        assert "Still two things." in content
        assert notify == rung("octocat", "alice", "bob")

    async def test_an_edit_keeps_the_files_and_rings_nobody(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """Read again, because the heading is drawn again; the histories are kept, so only the
        commits and the comments are asked twice."""
        on_two_files(github)
        await deliver(tracked, "pull_request_review", asking(), delivery="r1")
        written = len(threads.allowed)

        await deliver(
            tracked,
            "pull_request_review",
            asking("edited", body="Three things, inline."),
            delivery="r2",
        )

        assert len(threads.allowed) == written, "the edit was posted, or rang somebody"
        revised = threads.revisions[-1][2]
        assert "Three things, inline." in revised
        assert f"`{NOTES}` made by authors <@707>, <@808>" in revised
        assert len(github.commit_list_calls) == 2
        assert len(github.review_comment_calls) == 2
        assert len(github.file_history_calls) == 2

    async def test_a_review_github_delivers_twice_is_posted_and_rung_once(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        on_two_files(github)
        await deliver(tracked, "pull_request_review", asking(), delivery="r1")
        written = len(threads.allowed)

        await deliver(tracked, "pull_request_review", asking(), delivery="r1-again")

        assert await tracked.outcome_of("r1-again") == "processed"
        assert len(threads.allowed) == written, "the review was posted twice"


class TestInOneLine:
    async def test_a_review_with_no_inline_comments_rings_the_author_and_every_committer(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """No file to name, so the pull request's author first and then everybody who committed,
        in the order they did. No file is read."""
        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        content, notify = last_post(threads)
        assert content.startswith(
            "**<@606>** requested changes from <@707>, <@808>, <@1010>, <@909> <t:"
        )
        assert notify == rung("octocat", "alice", "dave", "bob")
        assert github.file_history_calls == []

    async def test_a_file_github_refuses_rings_everybody_at_once(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """A refusal says the same on every retry, so even a young review goes out at once, and
        the first refusal stops the reads behind it."""
        on_two_files(github)
        github.file_history_error = GitHubRefusedError("Resource not accessible by integration")

        await deliver(tracked, "pull_request_review", young(), delivery="r1")

        assert await tracked.outcome_of("r1") == "processed"
        assert await tracked.attempts_of("r1") == 0
        assert len(github.file_history_calls) == 1
        content, notify = last_post(threads)
        assert "requested changes from <@707>, <@808>, <@1010>, <@909>" in content
        assert notify == rung("octocat", "alice", "dave", "bob")

    async def test_a_young_review_waits_out_a_blip_and_names_the_files_once_github_answers(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """Held back before the claim, so the retry posts it rather than finding it said."""
        on_two_files(github)
        github.file_history_error = GitHubUnavailableError("GitHub is down")
        before = len(threads.posts)

        await deliver(tracked, "pull_request_review", young(), delivery="r1", expect_retries=True)

        assert await tracked.outcome_of("r1") == "pending"
        assert await tracked.attempts_of("r1") == 1
        assert len(threads.posts) == before, "the review went out without waiting"

        github.file_history_error = None
        await tracked.drain()

        assert len(threads.posts) == before + 1
        content, notify = last_post(threads)
        assert "requested changes on the files" in content
        assert notify == rung("octocat", "alice", "bob")


class TestWhatAsksGitHubNothing:
    async def test_an_approval(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """It has its own line, and the round-up under it reads the pull request, not this."""
        on_two_files(github)

        await deliver(tracked, "pull_request_review", payloads.pull_request_review_event())

        assert any("approved this pull request" in body for _, body in threads.posts)
        assert reads(github) == 0

    async def test_a_review_that_only_comments(
        self, tracked: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        on_two_files(github)

        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(state="commented", body="Two things, inline."),
        )

        content, notify = last_post(threads)
        assert "Two things, inline." in content
        assert notify == ()
        assert reads(github) == 0

    async def test_a_thread_with_comments_off(
        self,
        tracked: DeliveryClient,
        github: FakeGitHubClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Nothing is going to be posted, so there is nobody to ring and nothing to read for."""
        on_two_files(github)
        await track(
            db_sessionmaker,
            thread_id=threads.created[0].thread_id,
            off=(TrackedEvent.COMMENTS,),
        )

        await deliver(tracked, "pull_request_review", asking(), delivery="r1")

        assert await tracked.outcome_of("r1") == "processed"
        assert threads.posts == []
        assert reads(github) == 0

    async def test_a_pull_request_nobody_tracks(
        self, tracked: DeliveryClient, github: FakeGitHubClient
    ) -> None:
        payload = asking()
        payload["pull_request"]["number"] = 999

        await deliver(tracked, "pull_request_review", payload, delivery="r1")

        assert await tracked.outcome_of("r1") == "ignored"
        assert reads(github) == 0
