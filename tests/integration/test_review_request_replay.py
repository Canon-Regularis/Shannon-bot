"""A review request that has been answered stays answered, however late a delivery arrives.

GitHub drops a reviewer from `requested_reviewers` the moment they submit and sends no
`pull_request` event saying so, so the ledger closes the request rather than deleting the row.
Deleting is not safe: a retried `pull_request` delivery still lists the reviewer in its captured
payload, and would put the row back with `notified_at` already set, which silences every genuine
re-request for the life of the pull request.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, Repository, WebhookEvent
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.reviews import ReviewRequestLedger
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration

# The payload the first delivery was captured with. The review lands after it, and a person
# clicking re-request lands after that.
REQUESTED_AT = "2026-08-10T12:00:00Z"
RE_REQUESTED_AT = "2026-08-12T09:00:00Z"


def requests_a_review(**overrides):
    return payloads.pull_request_event("review_requested", **overrides)


def pings(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if "Review requested from" in body]


async def reviewers(session: AsyncSession) -> list[tuple[str, bool, bool]]:
    rows = await session.scalars(
        select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
    )
    return [
        (r.github_username, r.notified_at is not None, r.fulfilled_at is not None) for r in rows
    ]


async def let_the_backoff_elapse(container, delivery: str) -> None:
    async with container.sessionmaker() as session, session.begin():
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.github_delivery_id == delivery)
            .values(next_attempt_at=None)
        )


async def test_a_delivery_retried_after_the_review_neither_pings_nor_blocks_the_next_request(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        # The request arrives and the ping cannot be posted, so the delivery backs off with the
        # reviewer still owed one. Any failure in the Discord half does this; the ping is only
        # the easiest to arrange.
        real_post, refusing = threads.post, {"on": True}

        async def refuses(**kwargs):
            if refusing["on"]:
                raise DiscordGatewayError("Discord refused to post")
            return await real_post(**kwargs)

        threads.post = refuses
        await post(
            client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
        )
        await container.worker.run_once()
        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", False, False)]

        # She reviews it anyway. The ledger closes the request rather than removing it.
        refusing["on"] = False
        await post(
            client,
            "pull_request_review",
            payloads.pull_request_review_event("submitted"),
            delivery="rev-1",
        )
        await container.worker.run_once()
        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", False, True)]

        # The first delivery catches up. Its payload still lists her.
        await let_the_backoff_elapse(container, "req-1")
        await container.worker.run_once()

        assert pings(threads) == [], "she was asked to review what she had already approved"

        # Somebody clicks re-request. This is the one moment the ledger exists for.
        await post(
            client, "pull_request", requests_a_review(updated_at=RE_REQUESTED_AT), delivery="req-2"
        )
        await container.worker.run_once()

    assert len(pings(threads)) == 1, "a genuine re-request told nobody"


async def test_the_ordinary_request_and_review_still_work(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Nothing above should cost the plain path anything."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await post(
            client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
        )
        await container.worker.run_once()
        assert len(pings(threads)) == 1

        await post(
            client,
            "pull_request_review",
            payloads.pull_request_review_event("submitted"),
            delivery="rev-1",
        )
        await container.worker.run_once()
        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", True, True)]

        await post(
            client, "pull_request", requests_a_review(updated_at=RE_REQUESTED_AT), delivery="req-2"
        )
        await container.worker.run_once()

    assert len(pings(threads)) == 2, "re-requesting a review after one was given told nobody"


async def test_a_review_delivery_retried_after_a_re_request_does_not_close_it(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The other way round from the test above, and the one a review found.

    `build_note_handler` runs the ledger before the Discord post and again on every retry, and a
    review delivery has sixteen attempts across roughly two hours to be retried in. A re-request
    made inside that window was closed again by the next attempt, with the review's own
    timestamp, and the next ordinary event with a later one then read that stamp as an answered
    request, cleared it and pinged a third time for the second ask.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)
    review = parse_review_event("submitted", payloads.pull_request_review_event("submitted"))

    async with client:
        await post(
            client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
        )
        await container.worker.run_once()
        await ReviewRequestLedger(container.sessionmaker).fulfilled(review)

        await post(
            client, "pull_request", requests_a_review(updated_at=RE_REQUESTED_AT), delivery="req-2"
        )
        await container.worker.run_once()
        assert len(pings(threads)) == 2, "the second ask told nobody"

        # The review delivery is attempted again, which is all a retry does.
        await ReviewRequestLedger(container.sessionmaker).fulfilled(review)

        # Any handled action with a later timestamp. Labelling is the most ordinary there is.
        await post(
            client,
            "pull_request",
            payloads.pull_request_event("labeled", updated_at="2026-08-13T09:00:00Z"),
            delivery="lab-1",
        )
        await container.worker.run_once()

    assert len(pings(threads)) == 2, "an ordinary event pinged them again for a request still open"
    db_session.expunge_all()
    assert await reviewers(db_session) == [("monalisa", True, False)]


class TestAReviewLedgerWithNothingToDo:
    """Three ways in which there is nothing for the ledger to close.

    Each has to reach the guard it is named for, and say what happened to the request it left
    alone. Neither held before: the author one ran against a repository nobody had registered, so
    it returned two guards early and went on passing with the author guard deleted, and neither
    asserted anything at all, so any of them could have closed a request and still been green.
    """

    @pytest.fixture
    async def asked(self, registered: Repository, db_engine: AsyncEngine):
        """A pull request carrying one open review request, for the ledger to leave alone."""
        container = build_stack(db_engine, threads=FakeThreadGateway())
        client = build_http_client(container)
        async with client:
            await post(
                client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
            )
            await container.worker.run_once()
        return container

    async def test_a_review_with_no_author_is_left_alone(self, asked, db_session) -> None:
        """GitHub can report a deleted account as no author at all."""
        payload = payloads.pull_request_review_event()
        payload["review"]["user"] = None

        await ReviewRequestLedger(asked.sessionmaker).fulfilled(
            parse_review_event("submitted", payload)
        )

        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", True, False)]

    async def test_a_review_by_somebody_who_was_never_asked_is_left_alone(
        self, asked, db_session
    ) -> None:
        """Anybody with read access can review a pull request without being asked to, and the
        request that is open belongs to somebody else."""
        payload = payloads.pull_request_review_event()
        payload["review"]["user"] = payloads.user("hubot", 100)

        await ReviewRequestLedger(asked.sessionmaker).fulfilled(
            parse_review_event("submitted", payload)
        )

        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", True, False)]

    async def test_a_review_on_an_unregistered_repository_is_left_alone(
        self, db_sessionmaker, db_session
    ) -> None:
        snapshot = parse_review_event("submitted", payloads.pull_request_review_event())

        await ReviewRequestLedger(db_sessionmaker).fulfilled(snapshot)

        assert await reviewers(db_session) == []
