"""A review request that has been answered stays answered, however late a delivery arrives.

GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
`pull_request` event saying so, so the ledger follows the review and closes the request. It used
to do that by deleting the row, which the queue then undid: a `pull_request` delivery whose
Discord step failed is retried with the payload it was captured with, that payload still lists
the reviewer, and with the row gone the retry put the request back.

Two things came of that, and the second is the worse one. The thread got `Review requested from
monalisa.` posted directly under `**monalisa** approved this pull request`. And the row came back
with `notified_at` set, which is the exact state the ledger exists to prevent, so the next genuine
re-request found it already there and told nobody for the life of the pull request.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, Repository, WebhookEvent
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.enums import ActorRole
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import build_http_client, build_stack, send

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
        await send(
            client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
        )
        await container.worker.run_once()
        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", False, False)]

        # She reviews it anyway. The ledger closes the request rather than removing it.
        refusing["on"] = False
        await send(
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
        await send(
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
        await send(
            client, "pull_request", requests_a_review(updated_at=REQUESTED_AT), delivery="req-1"
        )
        await container.worker.run_once()
        assert len(pings(threads)) == 1

        await send(
            client,
            "pull_request_review",
            payloads.pull_request_review_event("submitted"),
            delivery="rev-1",
        )
        await container.worker.run_once()
        db_session.expunge_all()
        assert await reviewers(db_session) == [("monalisa", True, True)]

        await send(
            client, "pull_request", requests_a_review(updated_at=RE_REQUESTED_AT), delivery="req-2"
        )
        await container.worker.run_once()

    assert len(pings(threads)) == 2, "re-requesting a review after one was given told nobody"
