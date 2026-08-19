"""What a burst of deliveries leaves behind when Discord keeps failing part way through.

Every other test hands the gateway either a working day or one arranged failure. This one fails
a fixed fraction of every call it receives, which is the shape of a real bad afternoon: some
creates fail, some edits fail, some posts fail, and each of them lands at a different point in a
delivery that has already done part of its work.

The claims being made are the ones the design rests on. One item ends up with one thread and no
abandoned ones beside it, nobody is pinged twice, and nothing is left half-finished in the queue.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, Repository, TrackedItem, WebhookEvent
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.enums import ActorRole, DeliveryStatus
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration

DELIVERIES = 12


class FlakyGateway(FakeThreadGateway):
    """Fails every Nth call, whatever that call happens to be.

    Counted rather than random, so a failure found here can be run again and seen again.
    """

    def __init__(self, every: int) -> None:
        super().__init__()
        self.every = every
        self.calls = 0

    def _maybe_fail(self, what: str) -> None:
        self.calls += 1
        if self.calls % self.every == 0:
            raise DiscordGatewayError(f"Discord refused to {what} (call {self.calls})")

    async def create(self, **kwargs):
        self._maybe_fail("create")
        return await super().create(**kwargs)

    async def update(self, **kwargs):
        self._maybe_fail("update")
        return await super().update(**kwargs)

    async def post(self, **kwargs):
        self._maybe_fail("post")
        return await super().post(**kwargs)

    async def set_locked(self, **kwargs):
        self._maybe_fail("lock")
        return await super().set_locked(**kwargs)


async def drain(container, *, rounds: int = 400) -> None:
    """Work the queue out, including everything sitting on a backoff.

    An empty lease does not mean an empty queue: a delivery that failed is waiting on a timer.
    Pulling those forward is what makes this finish in a moment rather than in two hours.
    """
    for _ in range(rounds):
        if await container.worker.run_once():
            continue
        async with container.sessionmaker() as session, session.begin():
            woken = await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.status == DeliveryStatus.PENDING)
                .values(next_attempt_at=None)
            )
        if not woken.rowcount:
            return


@pytest.mark.parametrize("every", [2, 3, 5, 7])
async def test_a_flaky_gateway_leaves_one_thread_and_no_repeated_ping(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, every: int
) -> None:
    threads = FlakyGateway(every)
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        for n in range(DELIVERIES):
            await post(
                client,
                "pull_request",
                payloads.pull_request_event(
                    "edited", title=f"Title {n}", updated_at=f"2026-08-10T{12 + n}:00:00Z"
                ),
                delivery=f"flaky-{every}-{n}",
            )
        await drain(container)

    items = (await db_session.scalars(select(TrackedItem))).all()
    assert len(items) == 1, "a flaky gateway produced more than one tracked item"

    surviving = {
        thread_id for thread_id in threads.threads if thread_id not in set(threads.deleted)
    }
    assert len(surviving) == 1, f"{len(surviving)} threads left in the channel for one item"
    assert items[0].discord_thread_id in surviving, "the item points at a thread that is not there"

    pings = [body for _, body in threads.posts if "Review requested from" in body]
    assert len(pings) <= 1, f"the same reviewer was told about the same request {len(pings)} times"

    unfinished = await db_session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.status.in_([DeliveryStatus.PENDING, DeliveryStatus.PROCESSING]))
    )
    assert unfinished == 0, f"{unfinished} deliveries were left half-finished"


async def test_a_gateway_that_recovers_owes_nobody_a_ping(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """A claimed ping that could not be sent has to come back, every time, or it is lost.

    Separated from the test above because a delivery given up on after its sixteen attempts
    keeps its claim released and nothing ever sends it, which is the intended end of that road
    rather than a defect. Here the gateway stops failing, so every owed ping has to arrive.
    """
    threads = FlakyGateway(3)
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        for n in range(DELIVERIES):
            await post(
                client,
                "pull_request",
                payloads.pull_request_event(
                    "edited", title=f"Title {n}", updated_at=f"2026-08-10T{12 + n}:00:00Z"
                ),
                delivery=f"recovering-{n}",
            )
        await drain(container)
        threads.every = 10**9
        await drain(container)

    owed = await db_session.scalar(
        select(func.count())
        .select_from(ItemAssignment)
        .where(
            ItemAssignment.notified_at.is_(None),
            # Reviewers only: an assignee on a pull request is recorded and deliberately never
            # pinged, so their row keeps its null for ever and says nothing about this.
            ItemAssignment.role_type == ActorRole.REVIEWER,
        )
    )
    given_up = await db_session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.status == DeliveryStatus.FAILED)
    )
    assert (owed, given_up) == (0, 0), (
        f"{owed} reviewers left owed a ping, and {given_up} deliveries were abandoned"
    )
