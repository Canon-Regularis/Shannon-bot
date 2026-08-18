from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment, Repository
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_reviewer_ping
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.reviews import ReviewRequestLedger
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration


async def test_a_requested_reviewer_is_pinged_once(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    result = await notifying_sync_service.sync(pr_event("opened"))

    assert result is not None
    assert result.notified == ("monalisa",)
    assert len(threads.posts) == 1
    assert "monalisa" in threads.posts[0][1]


async def test_a_second_webhook_does_not_ping_again(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))
    result = await notifying_sync_service.sync(pr_event("edited", title="Renamed"))

    assert result is not None
    assert result.notified == ()
    assert len(threads.posts) == 1


async def test_a_newly_added_reviewer_is_pinged_but_the_first_is_not(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))

    result = await notifying_sync_service.sync(
        pr_event(
            "review_requested",
            requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
        )
    )

    assert result is not None
    assert result.notified == ("hubot",)
    assert len(threads.posts) == 2
    assert "hubot" in threads.posts[1][1]
    assert "monalisa" not in threads.posts[1][1]


async def test_a_linked_reviewer_is_pinged_by_mention(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="MonaLisa", discord_user_id=555
    )
    await db_session.commit()

    await notifying_sync_service.sync(pr_event("opened"))

    assert "<@555>" in threads.posts[0][1]


async def test_an_unlinked_reviewer_is_named_in_plain_text(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))

    assert "monalisa" in threads.posts[0][1]
    assert "<@" not in threads.posts[0][1]


async def test_a_linked_reviewer_appears_as_a_mention_in_the_metadata(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", discord_user_id=555
    )
    await db_session.commit()

    result = await notifying_sync_service.sync(pr_event("opened"))

    assert result is not None
    assert "**Reviewers:** <@555>" in threads.metadata_of(result.thread_id)


async def test_notification_stamps_the_assignment_row(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", discord_user_id=555
    )
    await db_session.commit()

    await notifying_sync_service.sync(pr_event("opened"))

    row = await db_session.scalar(
        select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
    )
    assert row is not None
    assert row.notified_at is not None
    assert row.discord_user_id == 555


async def test_a_removed_and_re_requested_reviewer_is_pinged_again(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))
    await notifying_sync_service.sync(pr_event("edited", requested_reviewers=[]))

    result = await notifying_sync_service.sync(
        pr_event("review_requested", requested_reviewers=[payloads.user("monalisa", 200)])
    )

    assert result is not None
    assert result.notified == ("monalisa",)
    assert len(threads.posts) == 2


async def test_a_pull_request_with_no_reviewers_pings_nobody(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    result = await notifying_sync_service.sync(pr_event("opened", requested_reviewers=[]))

    assert result is not None
    assert result.notified == ()
    assert threads.posts == []


class TestNobodyIsPingedTwice:
    """Two syncs of one item overlap whenever /pr is run while an event for it is in flight."""

    async def test_two_concurrent_syncs_post_one_ping(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        results = await asyncio.gather(
            notifying_sync_service.sync(pr_event("opened")),
            notifying_sync_service.sync(pr_event("review_requested")),
        )

        pinged = [logins for result in results for logins in result.notified]
        assert pinged == ["monalisa"]
        assert sum("Review requested" in content for _, content in threads.posts) == 1

    async def test_a_delivery_retried_after_the_ping_does_not_ping_again(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """The worker retries from the top, and the ping already went out."""
        await notifying_sync_service.sync(pr_event("opened"))

        again = await notifying_sync_service.sync(pr_event("opened"))

        assert again.notified == ()
        assert sum("Review requested" in content for _, content in threads.posts) == 1

    async def test_a_ping_that_never_went_out_is_still_owed(
        self,
        registered: Repository,
        db_sessionmaker,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """Claiming before posting must not swallow a ping when the post fails."""
        threads = _RefusingToPost()
        service = build_item_sync(
            db_sessionmaker,
            threads,
            PullRequestPolicy(),
            ActorNotifier(
                db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
        )

        with pytest.raises(DiscordGatewayError):
            await service.sync(pr_event("opened"))

        db_session.expire_all()
        row = await db_session.scalar(
            select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
        )
        assert row is not None and row.notified_at is None

    async def test_the_owed_ping_goes_out_on_the_retry(
        self,
        registered: Repository,
        db_sessionmaker,
        pr_event,
    ) -> None:
        threads = _RefusingToPost()
        service = build_item_sync(
            db_sessionmaker,
            threads,
            PullRequestPolicy(),
            ActorNotifier(
                db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
        )
        with pytest.raises(DiscordGatewayError):
            await service.sync(pr_event("opened"))

        threads.refusing = False
        result = await service.sync(pr_event("opened"))

        assert result.notified == ("monalisa",)


class _RefusingToPost(FakeThreadGateway):
    """A gateway that will open a thread but not post into it."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing = True

    async def post(self, *, thread_id: int, content: str) -> int | None:
        if self.refusing:
            raise DiscordGatewayError("Discord refused to post to the thread")
        return await super().post(thread_id=thread_id, content=content)


class TestReRequestingAReviewAfterOneWasGiven:
    """The single moment the reviewer ping exists for, and it used to say nothing at all.

    GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
    `pull_request` event saying so. Nothing else in the sequence tells us either: the author's
    push arrives as `synchronize`, which is deliberately not handled.
    """

    async def test_the_reviewer_is_pinged_again(
        self,
        registered: Repository,
        db_sessionmaker,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await notifying_sync_service.sync(pr_event("opened"))
        await ReviewRequestLedger(db_sessionmaker).fulfilled(
            parse_review_event("submitted", payloads.pull_request_review_event())
        )

        # Later than the review, because that is the only thing a request made after one can be.
        # A closed request is reopened by a payload newer than the review that closed it, which
        # is what separates somebody clicking re-request from a delivery still retrying from
        # before it. That rests on GitHub advancing `pull_request.updated_at` when a review is
        # requested, which it does because requesting one changes the pull request. If that ever
        # turns out to be wrong, this is the test that says so, and the fix is to find another
        # way to tell the two apart rather than to widen the comparison.
        result = await notifying_sync_service.sync(
            pr_event("review_requested", updated_at="2026-08-12T09:00:00Z")
        )

        assert result.notified == ("monalisa",)
        assert sum("Review requested" in content for _, content in threads.posts) == 2

    async def test_the_request_is_closed_when_the_review_lands(
        self,
        registered: Repository,
        db_sessionmaker,
        db_session: AsyncSession,
        notifying_sync_service: ItemSyncService,
        pr_event,
    ) -> None:
        """Closed, not removed.

        Removing it let a `pull_request` delivery retried after the review put the request
        straight back, because the payload it was captured with still lists the reviewer. What
        matters is that the request is answered, and the stamp is what says so to anything that
        arrives later.
        """
        await notifying_sync_service.sync(pr_event("opened"))

        await ReviewRequestLedger(db_sessionmaker).fulfilled(
            parse_review_event("submitted", payloads.pull_request_review_event())
        )

        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
            )
        ).all()
        assert [(row.github_username, row.fulfilled_at is not None) for row in rows] == [
            ("monalisa", True)
        ]

    async def test_somebody_else_reviewing_leaves_the_request_alone(
        self,
        registered: Repository,
        db_sessionmaker,
        db_session: AsyncSession,
        notifying_sync_service: ItemSyncService,
        pr_event,
    ) -> None:
        await notifying_sync_service.sync(pr_event("opened"))
        payload = payloads.pull_request_review_event()
        payload["review"]["user"] = payloads.user("someone-else", 900)

        await ReviewRequestLedger(db_sessionmaker).fulfilled(
            parse_review_event("submitted", payload)
        )

        db_session.expire_all()
        row = await db_session.scalar(
            select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
        )
        assert row is not None and row.github_username == "monalisa"

    async def test_a_review_on_something_untracked_does_nothing(
        self, registered: Repository, db_sessionmaker, pr_event
    ) -> None:
        payload = payloads.pull_request_review_event()
        payload["pull_request"]["number"] = 999

        await ReviewRequestLedger(db_sessionmaker).fulfilled(
            parse_review_event("submitted", payload)
        )


class TestAPingInterruptedMidFlight:
    """The worker cancels a handler when its deadline passes, often inside the Discord call.

    discord.py sleeps through a rate limit rather than failing, so the post is exactly where a
    delivery tends to be when its sixty seconds run out.
    """

    async def test_a_cancelled_post_leaves_the_ping_owed(
        self, registered: Repository, db_sessionmaker, db_session: AsyncSession, pr_event
    ) -> None:
        threads = _HangingOnPost()
        service = _notifying(db_sessionmaker, threads)

        # TimeoutError is what wait_for turns the cancellation into, and what the worker sees.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(service.sync(pr_event("opened")), timeout=0.2)

        # The hand-back is shielded, so the cancellation returns here first and it lands a
        # moment later on its own. That it happens is the point; that it is instant is not.
        assert await _owed_again(db_session), "the ping was claimed and never handed back"

    async def test_the_owed_ping_goes_out_on_the_retry(
        self, registered: Repository, db_sessionmaker, pr_event
    ) -> None:
        threads = _HangingOnPost()
        service = _notifying(db_sessionmaker, threads)
        # TimeoutError is what wait_for turns the cancellation into, and what the worker sees.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(service.sync(pr_event("opened")), timeout=0.2)

        threads.hanging = False
        result = await service.sync(pr_event("opened"))

        assert result.notified == ("monalisa",)


def _notifying(db_sessionmaker, threads: FakeThreadGateway) -> ItemSyncService:
    return build_item_sync(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
        ),
    )


class _HangingOnPost(FakeThreadGateway):
    """A gateway whose post never returns, the way a rate-limited one behaves."""

    def __init__(self) -> None:
        super().__init__()
        self.hanging = True

    async def post(self, *, thread_id: int, content: str) -> int | None:
        if self.hanging:
            await asyncio.sleep(60)
        return await super().post(thread_id=thread_id, content=content)


async def _owed_again(session: AsyncSession, timeout: float = 5.0) -> bool:
    """Whether the reviewer's ping is unclaimed again, waiting briefly for the hand-back."""
    async with asyncio.timeout(timeout):
        while True:
            session.expire_all()
            row = await session.scalar(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
            )
            if row is not None and row.notified_at is None:
                return True
            await asyncio.sleep(0.02)
