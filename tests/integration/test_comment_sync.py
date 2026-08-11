from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ObjectType, Status
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import map_channel, register_repository
from tests.support.stack import build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    """An issue and a pull request, both already synced."""
    repository = await register_repository(db_session, guild_id=1, channel_id=99)
    await map_channel(db_session, repository, ObjectType.ISSUE, channel_id=98)
    container = build_stack(db_engine, threads=threads)
    async with build_http_client(container) as http_client:
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


def thread_for(threads: FakeThreadGateway, channel_id: int) -> int:
    return next(t.thread_id for t in threads.created if t.channel_id == channel_id)


async def test_a_comment_on_an_issue_reaches_its_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(
        tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1"
    )

    assert response.json()["status"] == "accepted"
    posted = [
        content for thread_id, content in threads.posts if thread_id == thread_for(threads, 98)
    ]
    assert any("monalisa" in content for content in posted)


async def test_the_comment_carries_everything_the_issue_asks_for(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    content = threads.posts[-1][1]
    assert "**monalisa** commented" in content
    assert "<t:" in content
    assert "> Reproduced on main" in content
    assert f"issuecomment-{payloads.COMMENT_ID}" in content


async def test_a_comment_on_a_pull_request_reaches_the_pull_request_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    """The comment payload reports the issue id, which never matches the stored pull request id.

    Matching on number is the only reason this lands anywhere.
    """
    payload = payloads.issue_comment_event(on=payloads.pull_request_as_issue())

    response = await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert response.json()["status"] == "accepted"
    assert threads.posts[-1][0] == thread_for(threads, 99)


async def test_a_linked_commenter_is_mentioned(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", discord_user_id=909
    )
    await db_session.commit()

    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    assert "<@909>" in threads.posts[-1][1]


async def test_a_comment_on_an_untracked_item_is_ignored(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event(on=payloads.issue(id=999, number=999))

    await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert await tracked.outcome_of("c1") == "ignored"
    assert len(threads.posts) == before


async def test_a_comment_from_another_repository_is_ignored(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event()
    payload["repository"]["id"] = 999999

    await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert await tracked.outcome_of("c1") == "ignored"
    assert len(threads.posts) == before


async def test_comments_never_duplicate_the_metadata_message(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    thread_id = thread_for(threads, 98)
    metadata_before = threads.metadata_of(thread_id)
    message_before = await db_session.scalar(
        select(TrackedItem.discord_message_id).where(
            TrackedItem.github_object_type == ObjectType.ISSUE
        )
    )

    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    db_session.expunge_all()
    message_after = await db_session.scalar(
        select(TrackedItem.discord_message_id).where(
            TrackedItem.github_object_type == ObjectType.ISSUE
        )
    )
    assert message_after == message_before
    assert threads.metadata_of(thread_id) == metadata_before
    assert len(threads.created) == 2


async def test_a_repeated_comment_delivery_posts_once(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event()

    first = await deliver(tracked, "issue_comment", payload, delivery="c1")
    second = await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.posts) == before + 1


async def test_an_edited_comment_is_not_mirrored(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)

    response = await deliver(
        tracked, "issue_comment", payloads.issue_comment_event("edited"), delivery="c1"
    )

    assert response.json()["status"] == "ignored"
    assert len(threads.posts) == before


async def test_a_comment_on_a_closed_issue_still_lands(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    """Closing locks the thread, and the bot still has to be able to write to it."""
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z"),
        delivery="i1",
    )
    before = len(threads.posts)

    response = await deliver(
        tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1"
    )

    assert response.json()["status"] == "accepted"
    assert len(threads.posts) == before + 1


async def test_the_number_of_tracked_items_never_changes(
    tracked: AsyncClient, db_session: AsyncSession
) -> None:
    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 2


class TestReviewMirroring:
    async def test_a_submitted_review_reaches_the_pull_request_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        response = await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert response.json()["status"] == "accepted"
        assert threads.posts[-1][0] == thread_for(threads, 99)
        assert "**monalisa** approved this pull request" in threads.posts[-1][1]

    async def test_changes_requested_reaches_the_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(state="changes_requested"),
            delivery="r1",
        )

        assert "requested changes" in threads.posts[-1][1]

    async def test_an_approval_with_no_body_still_lands(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)

        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(body=""),
            delivery="r1",
        )

        assert len(threads.posts) == before + 1
        assert "approved this pull request" in threads.posts[-1][1]

    async def test_a_linked_reviewer_is_mentioned(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", discord_user_id=606
        )
        await db_session.commit()

        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert "<@606>" in threads.posts[-1][1]

    async def test_a_review_on_an_untracked_pull_request_is_ignored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)
        payload = payloads.pull_request_review_event()
        payload["pull_request"]["number"] = 999

        await deliver(tracked, "pull_request_review", payload, delivery="r1")

        assert await tracked.outcome_of("r1") == "ignored"
        assert len(threads.posts) == before

    async def test_a_dismissed_review_is_not_mirrored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)

        response = await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event("dismissed"),
            delivery="r1",
        )

        assert response.json()["status"] == "ignored"
        assert len(threads.posts) == before

    async def test_a_repeated_review_delivery_posts_once(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)
        payload = payloads.pull_request_review_event()

        first = await deliver(tracked, "pull_request_review", payload, delivery="r1")
        second = await deliver(tracked, "pull_request_review", payload, delivery="r1")

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"
        assert len(threads.posts) == before + 1

    async def test_a_review_never_touches_the_metadata_message(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        thread_id = thread_for(threads, 99)
        before = threads.metadata_of(thread_id)

        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert threads.metadata_of(thread_id) == before

    async def test_a_review_does_not_move_the_workflow_status(
        self, tracked: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Approving is not /SET_READY_FOR_MERGE. Status commands arrive in MVP 3."""
        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        db_session.expunge_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item is not None
        assert item.status is Status.NOT_REVIEWED


class TestNoteTargeting:
    async def test_a_comment_is_matched_on_kind_as_well_as_number(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Numbers are unique per repository on GitHub, so this can only bite on bad data.

        Pinning the kind means a comment can never be handed the other sort of item.
        """
        from shannon.db.stores.repositories import RepositoryStore
        from shannon.db.stores.tracked_items import TrackedItemStore

        repository = await RepositoryStore(db_session).get_by_github_id(payloads.REPO_ID)
        assert repository is not None
        items = TrackedItemStore(db_session)

        issue = await items.get_by_number(
            repository_id=repository.id, number=12, object_type=ObjectType.ISSUE
        )
        pull = await items.get_by_number(
            repository_id=repository.id, number=7, object_type=ObjectType.PR
        )

        assert issue is not None and issue.github_object_type is ObjectType.ISSUE
        assert pull is not None and pull.github_object_type is ObjectType.PR
        assert issue.id != pull.id

    async def test_asking_for_the_wrong_kind_finds_nothing(
        self, tracked: AsyncClient, db_session: AsyncSession
    ) -> None:
        from shannon.db.stores.repositories import RepositoryStore
        from shannon.db.stores.tracked_items import TrackedItemStore

        repository = await RepositoryStore(db_session).get_by_github_id(payloads.REPO_ID)
        assert repository is not None

        wrong = await TrackedItemStore(db_session).get_by_number(
            repository_id=repository.id, number=12, object_type=ObjectType.PR
        )

        assert wrong is None
