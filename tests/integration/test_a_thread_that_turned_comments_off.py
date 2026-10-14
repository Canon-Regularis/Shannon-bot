"""A thread that turned `comments` off hears no comment, review or reply. Issue #163.

Only the line goes. Everything that rides on a note still happens: a submitted review still closes
the request that asked for it, and the round-up under an approval still says when every review has
come back approving, because that has a switch of its own. And nothing is recorded as said, so a
note that went unsaid stays a note this thread has never seen.

Driven through the stack the container builds, because the two hooks either side of the mirror are
wiring, and a test of the mirror alone could not see either of them run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import ItemAssignment, MirroredNote
from shannon.domain.enums import ActorRole
from shannon.domain.models import Actor, ReviewSnapshot
from shannon.domain.tracking import TrackedEvent
from shannon.github import mapping
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import track
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()
ROUND_UP = "### 🏁 Approved"


def a_github() -> FakeGitHubClient:
    """A GitHub holding the pull request with nobody left to answer, and one approval on it.

    What the round-up asks for. Without it an approval's delivery fails after the note, which
    is the wrong reason for anything here to be quiet.
    """
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    snapshot = mapping.pull_request(payloads.pull_request(requested_reviewers=[]), repo)
    assert snapshot is not None
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): snapshot})
    github.reviews[(REPO_FULL, 7)] = [
        ReviewSnapshot(
            repository=repo,
            item_number=7,
            review_id=1,
            state="APPROVED",
            author=Actor("monalisa", 200),
            body="",
            html_url="",
            created_at=datetime(2026, 8, 11, 11, 0, tzinfo=UTC),
        )
    ]
    return github


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[tuple[DeliveryClient, FakeGitHubClient]]:
    """A pull request that already has its thread, with comments still on."""
    github = a_github()
    async with registered_stack(db_engine, db_session, threads, github=github) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client, github


async def comments(
    sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway, *, on: bool
) -> None:
    """What `/track comments on` or `/track comments off` leaves behind in the thread."""
    chosen = (TrackedEvent.COMMENTS,)
    thread_id = threads.created[0].thread_id
    await track(
        sessionmaker, thread_id=thread_id, on=chosen if on else (), off=() if on else chosen
    )


def a_comment(action: str = "created", **overrides: object) -> dict:
    """A comment on the pull request, which GitHub sends as a comment on an issue."""
    return payloads.issue_comment_event(action, on=payloads.pull_request_as_issue(), **overrides)


async def held(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count()).select_from(MirroredNote)) or 0


class TestWhatGoesUnsaid:
    async def test_a_comment_is_not_posted_and_nothing_is_claimed(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        client, _ = tracked
        await comments(db_sessionmaker, threads, on=False)

        await deliver(client, "issue_comment", a_comment(), delivery="c1")

        assert await client.outcome_of("c1") == "processed"
        assert threads.posts == []
        assert await held(db_session) == 0, "a note that was never said was recorded as said"

    async def test_a_reply_on_a_diff_asks_github_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Who a reply answers is read from GitHub before it is posted, issue #231. A reply that
        is not going to be posted has nobody to ring, so the read would be for nothing."""
        client, github = tracked
        await comments(db_sessionmaker, threads, on=False)

        await deliver(
            client,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(in_reply_to_id=4197046428),
            delivery="r1",
        )

        assert await client.outcome_of("r1") == "processed"
        assert threads.posts == []
        assert github.review_comment_calls == []

    async def test_an_edit_to_a_note_already_in_the_thread_leaves_it_as_it_was(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rewriting it is saying something too, so off means the message keeps its old text."""
        client, _ = tracked
        await deliver(client, "issue_comment", a_comment(), delivery="c1")
        assert len(threads.posts) == 1, "the comment never reached the thread to begin with"
        await comments(db_sessionmaker, threads, on=False)

        await deliver(
            client,
            "issue_comment",
            a_comment("edited", body="Not on main after all"),
            delivery="c2",
        )

        assert threads.revisions == []


class TestWhatStillHappens:
    async def test_an_approval_still_closes_its_request_and_still_gets_its_round_up(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The two hooks either side of the post, both of which outlive the post itself."""
        client, _ = tracked
        await comments(db_sessionmaker, threads, on=False)

        await deliver(
            client, "pull_request_review", payloads.pull_request_review_event(), delivery="v1"
        )

        said = [body for _, body in threads.posts]
        assert len(said) == 1, f"expected the round-up alone, got {said}"
        assert ROUND_UP in said[0]
        fulfilled = await db_session.scalar(
            select(ItemAssignment.fulfilled_at).where(
                ItemAssignment.role_type == ActorRole.REVIEWER,
                ItemAssignment.github_username == "monalisa",
            )
        )
        assert fulfilled is not None, "the review closed nothing"

    async def test_an_edit_after_comments_are_back_on_is_posted_fresh(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Nothing was recorded for the original, so its edit arrives as a note this thread has
        never seen and is posted - the gap repair any note that missed its thread gets."""
        client, _ = tracked
        await comments(db_sessionmaker, threads, on=False)
        await deliver(client, "issue_comment", a_comment(), delivery="c1")
        await comments(db_sessionmaker, threads, on=True)

        await deliver(
            client,
            "issue_comment",
            a_comment("edited", body="Not on main after all"),
            delivery="c2",
        )

        assert threads.revisions == []
        assert len(threads.posts) == 1
        assert "Not on main after all" in threads.posts[0][1]
