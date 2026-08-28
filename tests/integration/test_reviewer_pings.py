from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment, Repository
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_reviewer_ping
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.pull_request import parse_pull_request_event
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
        guild_id=1, github_username="MonaLisa", github_user_id=200, discord_user_id=555
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
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
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
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
    )
    await db_session.commit()

    await notifying_sync_service.sync(pr_event("opened"))

    row = await db_session.scalar(
        select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
    )
    assert row is not None
    assert row.notified_at is not None


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

        await _cancelled_in_the_ping(service, threads, pr_event("opened"))

        # The hand-back is shielded, so it commits on its own schedule rather than before the
        # cancellation comes back. That it happens is the point; that it is instant is not.
        assert await _owed_again(db_session), "the ping was claimed and never handed back"

    async def test_the_owed_ping_goes_out_on_the_retry(
        self, registered: Repository, db_sessionmaker, db_session: AsyncSession, pr_event
    ) -> None:
        threads = _HangingOnPost()
        service = _notifying(db_sessionmaker, threads)
        await _cancelled_in_the_ping(service, threads, pr_event("opened"))
        assert await _owed_again(db_session), "the ping was claimed and never handed back"

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
    """A gateway whose post never returns, the way a rate-limited one behaves.

    `posting` is set on the way in, so a test can cancel once the sync has reached the ping
    rather than after a wait it hopes is long enough.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hanging = True
        self.posting = asyncio.Event()

    async def post(self, *, thread_id: int, content: str) -> int | None:
        self.posting.set()
        if self.hanging:
            await asyncio.sleep(60)
        return await super().post(thread_id=thread_id, content=content)


async def _cancelled_in_the_ping(service: ItemSyncService, threads: _HangingOnPost, snapshot):
    """Cancel a sync where the worker's deadline usually lands, which is inside the post.

    Driven off the gateway rather than off a clock. Everything before the ping is several
    database round trips and a thread creation, and on a loaded machine that outlasts a deadline
    short enough to be worth writing, which puts the cancellation somewhere these tests are not
    about and leaves them passing for the wrong reason. The worker uses `wait_for`, so what it
    sees is a TimeoutError; what the notifier sees is the cancellation either way, and that is
    the half under test here.
    """
    syncing = asyncio.create_task(service.sync(snapshot))
    await asyncio.wait_for(threads.posting.wait(), timeout=10)
    syncing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await syncing


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


class TestHandingAClaimBackWhateverCaseItArrivesIn:
    """The hand-back matches on the login, and matching nothing is the worst outcome here.

    A ping is claimed before it is sent, so a row whose claim is not handed back is stamped as
    told while nobody was told, and nothing revisits it. The column holds logins folded, because
    the one thing that writes it folds on the way in, and the three other methods on this store
    that take logins fold before comparing. This one did not, and it worked only because its one
    caller hands back exactly what the claim returned, which came out of that column already
    folded.
    """

    async def test_a_login_in_the_case_github_uses_still_finds_its_row(
        self, registered: Repository, db_sessionmaker, db_session: AsyncSession, pr_event
    ) -> None:
        service = build_item_sync(db_sessionmaker, FakeThreadGateway(), PullRequestPolicy())
        await service.sync(pr_event("opened"))
        item_id = await db_session.scalar(select(ItemAssignment.tracked_item_id))
        store = ItemAssignmentStore

        async with db_sessionmaker() as session, session.begin():
            claimed = await store(session).claim_notifications(item_id, ActorRole.REVIEWER)
        assert list(claimed) == ["monalisa"], "nothing was claimed, so this proves nothing"

        # What GitHub calls them, which is not what the column holds.
        async with db_sessionmaker() as session, session.begin():
            await store(session).release_notifications(item_id, ActorRole.REVIEWER, ["MonaLisa"])

        db_session.expire_all()
        row = await db_session.scalar(
            select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
        )
        assert row.notified_at is None, "the claim was never handed back, so the ping is lost"


class TestAReviewerWhoseLoginSomebodyElseNowHolds:
    """The ping is the mention that notifies, and it is the one built from a stored name.

    Everywhere else a mention is built the payload is in hand and carries the account's id. The
    ping resolves from `item_assignments` long after that payload has gone, so without the id
    recorded on the row it is the one place a stranger who took a freed login inherits somebody
    else's Discord account and gets a real notification for a review nobody asked them for.
    """

    async def test_the_stranger_is_named_rather_than_mentioned(
        self,
        registered: Repository,
        db_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        # Alice linked when `monalisa` was hers, under the id she had then.
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", github_user_id=111, discord_user_id=555
        )
        await db_session.commit()
        service = build_item_sync(
            db_sessionmaker,
            threads,
            PullRequestPolicy(),
            ActorNotifier(
                db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
        )

        # The payload's `monalisa` is account 200, which is somebody else.
        await service.sync(pr_event("opened"))

        assert threads.posts == [(threads.created[0].thread_id, "Review requested from monalisa.")]
        assert "<@555>" not in threads.metadata_of(threads.created[0].thread_id)

    async def test_the_person_who_linked_is_still_mentioned(
        self,
        registered: Repository,
        db_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
        )
        await db_session.commit()
        service = build_item_sync(
            db_sessionmaker,
            threads,
            PullRequestPolicy(),
            ActorNotifier(
                db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
            ),
        )

        await service.sync(pr_event("opened"))

        assert threads.posts == [(threads.created[0].thread_id, "Review requested from <@555>.")]


class TestAReviewerWhoRenamedTheirAccount:
    """A rename is one person, and matching on the name read it as two.

    `replace` makes the stored people match the payload, and it matched on the login alone. So a
    reviewer renaming their GitHub account looked like the one who was asked leaving and a
    stranger arriving: the row was deleted and a fresh one inserted with `notified_at` empty, and
    the next ordinary event on the item announced a review request nobody had re-made.

    Renaming is ordinary housekeeping, and the announcement is not free. Where the new name is
    already linked it tells the same person twice for one request, which is the one thing
    `notified_at` exists to stop.
    """

    async def test_the_request_row_survives_it(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await notifying_sync_service.sync(pr_event("opened"))
        told = len(threads.posts)

        # The same account, id 200, under the name GitHub uses now.
        renamed = payloads.pull_request_event("labeled")
        renamed["pull_request"]["requested_reviewers"] = [payloads.user("mona-lisa", 200)]
        await notifying_sync_service.sync(parse_pull_request_event("labeled", renamed))

        assert threads.posts[told:] == [], "it announced a request nobody made again"
        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
            )
        ).all()
        assert len(rows) == 1, "the rename left two rows for one person"
        assert rows[0].github_username == "mona-lisa", "the row kept the name they left behind"
        assert rows[0].notified_at is not None, "the record of having told them was thrown away"

    async def test_two_of_them_swapping_names_does_not_fail_the_delivery(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        """One reviewer takes the name another has just freed, and both are on this item.

        GitHub frees a name the moment it is left, so this needs nothing unusual: 300 renames,
        200 takes what 300 left, and both happened between two events on the item, so both
        arrive together. Following a rename by giving the row its new name then wrote a name the
        other row was still holding, and the unique constraint refused it. That raises out of
        the delivery rather than being handled anywhere, so the item stops mirroring entirely
        until sixteen attempts have run out over two hours.

        The names are what they are afterwards, and neither is told again, which is the whole
        reason for following a rename at all.
        """
        await notifying_sync_service.sync(
            pr_event(
                "opened",
                requested_reviewers=[payloads.user("mona", 200), payloads.user("hub", 300)],
            )
        )
        told = len(threads.posts)

        await notifying_sync_service.sync(
            pr_event(
                "labeled",
                requested_reviewers=[payloads.user("hub", 200), payloads.user("mona", 300)],
            )
        )

        assert threads.posts[told:] == [], "it announced a request nobody made again"
        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
            )
        ).all()
        assert sorted((row.github_user_id, row.github_username) for row in rows) == [
            (200, "hub"),
            (300, "mona"),
        ]
        assert all(row.notified_at is not None for row in rows), "a record of telling them went"

    async def test_somebody_genuinely_leaving_still_loses_their_row(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The other side of it: a different account is a different person, whatever it is
        called, and the row that says they were asked has to go."""
        await notifying_sync_service.sync(pr_event("opened"))

        await notifying_sync_service.sync(
            pr_event("labeled", requested_reviewers=[payloads.user("somebody-else", 900)])
        )

        db_session.expire_all()
        rows = (
            await db_session.scalars(
                select(ItemAssignment.github_username).where(
                    ItemAssignment.role_type == ActorRole.REVIEWER
                )
            )
        ).all()
        assert sorted(rows) == ["somebody-else"]
