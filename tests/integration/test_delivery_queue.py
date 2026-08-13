from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shannon.db.models import WebhookEvent
from shannon.domain.enums import DeliveryStatus
from shannon.services.delivery_queue import Delivery, WebhookDeliveryQueue

pytestmark = pytest.mark.integration

PAYLOAD = {"action": "opened", "number": 7}
LEASE = timedelta(minutes=5)


@pytest.fixture
def queue(db_sessionmaker: async_sessionmaker) -> WebhookDeliveryQueue:
    return WebhookDeliveryQueue(db_sessionmaker)


async def count_events(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(WebhookEvent)) or 0


async def row(session: AsyncSession, delivery_id: str) -> WebhookEvent:
    session.expire_all()
    event = await session.scalar(
        select(WebhookEvent).where(WebhookEvent.github_delivery_id == delivery_id)
    )
    assert event is not None
    return event


async def test_the_first_enqueue_wins_and_the_second_loses(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    assert await queue.enqueue("delivery-a", "pull_request", PAYLOAD) is True
    assert await queue.enqueue("delivery-a", "pull_request", PAYLOAD) is False

    assert await count_events(db_session) == 1


async def test_concurrent_enqueues_produce_exactly_one_winner(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    results = await asyncio.gather(
        *(queue.enqueue("delivery-a", "pull_request", PAYLOAD) for _ in range(8))
    )

    assert results.count(True) == 1
    assert await count_events(db_session) == 1


async def test_an_enqueued_delivery_records_its_body_and_hash(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    """The payload is what makes a retry possible, and the hash is what proves it arrived intact."""
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)

    event = await row(db_session, "delivery-a")
    canonical = json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
    assert event.payload == PAYLOAD
    assert event.payload_hash == hashlib.sha256(canonical).hexdigest()
    assert event.event_type == "pull_request"
    assert event.status == DeliveryStatus.PENDING
    assert event.attempts == 0


async def test_leasing_returns_the_delivery_and_marks_it_processing(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)

    leased = await queue.lease(limit=10, lease_for=LEASE)

    assert [delivery.delivery_id for delivery in leased] == ["delivery-a"]
    assert leased[0].payload == PAYLOAD
    assert leased[0].action == "opened"
    assert (await row(db_session, "delivery-a")).status == DeliveryStatus.PROCESSING


async def test_a_leased_delivery_is_not_leased_again(queue: WebhookDeliveryQueue) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    await queue.lease(limit=10, lease_for=LEASE)

    assert await queue.lease(limit=10, lease_for=LEASE) == []


async def test_deliveries_are_leased_in_the_order_they_arrived(
    queue: WebhookDeliveryQueue,
) -> None:
    """Two events for the same item have to be acted on in order or the later one is undone."""
    for name in ("first", "second", "third"):
        await queue.enqueue(name, "issues", PAYLOAD)

    leased = await queue.lease(limit=10, lease_for=LEASE)

    assert [delivery.delivery_id for delivery in leased] == ["first", "second", "third"]


async def test_leasing_respects_the_limit(queue: WebhookDeliveryQueue) -> None:
    for index in range(5):
        await queue.enqueue(f"delivery-{index}", "issues", PAYLOAD)

    assert len(await queue.lease(limit=2, lease_for=LEASE)) == 2


async def test_finishing_records_the_status_and_drops_the_lease(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)

    await queue.finish(leased[0], DeliveryStatus.PROCESSED)

    event = await row(db_session, "delivery-a")
    assert event.status == DeliveryStatus.PROCESSED
    assert event.processed_at is not None
    assert event.locked_until is None


async def test_retrying_later_puts_the_delivery_back_with_a_reason(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)

    await queue.retry_later(leased[0], error="RuntimeError: discord is down", delay=timedelta(0))

    event = await row(db_session, "delivery-a")
    assert event.status == DeliveryStatus.PENDING
    assert event.attempts == 1
    assert event.last_error == "RuntimeError: discord is down"
    assert event.locked_until is None


async def test_a_delivery_waiting_on_its_backoff_is_not_leased(
    queue: WebhookDeliveryQueue,
) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)
    await queue.retry_later(leased[0], error="down", delay=timedelta(hours=1))

    assert await queue.lease(limit=10, lease_for=LEASE) == []


async def test_a_delivery_past_its_backoff_is_leased_again(queue: WebhookDeliveryQueue) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)
    await queue.retry_later(leased[0], error="down", delay=timedelta(seconds=-1))

    again = await queue.lease(limit=10, lease_for=LEASE)

    assert [delivery.delivery_id for delivery in again] == ["delivery-a"]
    assert again[0].attempts == 1


async def test_giving_up_marks_the_delivery_failed(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)

    await queue.give_up(leased[0], error="RuntimeError: still down")

    event = await row(db_session, "delivery-a")
    assert event.status == DeliveryStatus.FAILED
    assert event.last_error == "RuntimeError: still down"
    assert event.processed_at is not None


async def test_a_failed_delivery_is_never_leased_again(queue: WebhookDeliveryQueue) -> None:
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)
    await queue.give_up(leased[0], error="gave up")

    assert await queue.lease(limit=10, lease_for=LEASE) == []


async def test_a_delivery_stranded_by_a_dead_worker_is_taken_back(
    queue: WebhookDeliveryQueue,
) -> None:
    """A worker killed mid-delivery leaves its row PROCESSING. The lease is what unsticks it."""
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    await queue.lease(limit=10, lease_for=timedelta(seconds=-1))

    recovered = await queue.lease(limit=10, lease_for=LEASE)

    assert [delivery.delivery_id for delivery in recovered] == ["delivery-a"]


async def test_a_row_written_before_this_was_a_queue_is_never_leased(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    """Old rows have no body, so acting on one is impossible. It must not be picked up."""
    await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
    await db_session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.github_delivery_id == "delivery-a")
        .values(
            payload=None,
            status=DeliveryStatus.PROCESSING,
            locked_until=datetime.now(tz=UTC) - timedelta(days=1),
        )
    )
    await db_session.commit()

    assert await queue.lease(limit=10, lease_for=LEASE) == []


async def test_two_workers_never_lease_the_same_delivery(
    queue: WebhookDeliveryQueue, prepared_database: str
) -> None:
    """SKIP LOCKED is what makes a second replica safe rather than a source of duplicates."""
    for index in range(6):
        await queue.enqueue(f"delivery-{index}", "issues", PAYLOAD)

    engines: list[AsyncEngine] = [create_async_engine(prepared_database) for _ in range(2)]
    try:
        rivals = [
            WebhookDeliveryQueue(async_sessionmaker(engine, expire_on_commit=False))
            for engine in engines
        ]
        batches = await asyncio.gather(*(rival.lease(limit=6, lease_for=LEASE) for rival in rivals))
    finally:
        for engine in engines:
            await engine.dispose()

    taken = [delivery.delivery_id for batch in batches for delivery in batch]
    assert sorted(taken) == [f"delivery-{index}" for index in range(6)]
    assert len(set(taken)) == len(taken)


async def test_pruning_removes_old_finished_deliveries(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("old", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)
    await queue.finish(leased[0], DeliveryStatus.PROCESSED)
    await db_session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.github_delivery_id == "old")
        .values(processed_at=datetime.now(tz=UTC) - timedelta(days=30))
    )
    await db_session.commit()

    assert await queue.prune(keep_for=timedelta(days=7)) == 1
    assert await count_events(db_session) == 0


async def test_pruning_leaves_recent_and_pending_deliveries_alone(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    await queue.enqueue("recent", "pull_request", PAYLOAD)
    leased = await queue.lease(limit=10, lease_for=LEASE)
    await queue.finish(leased[0], DeliveryStatus.PROCESSED)
    await queue.enqueue("waiting", "pull_request", PAYLOAD)

    assert await queue.prune(keep_for=timedelta(days=7)) == 0
    assert await count_events(db_session) == 2


class TestRedeliveringSomethingThatFailed:
    """GitHub's Redeliver button sends the same delivery id, which used to read as a duplicate."""

    async def test_a_failed_delivery_is_put_back_on_the_queue(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
        leased = await queue.lease(limit=10, lease_for=LEASE)
        await queue.give_up(leased[0], error="Discord would not have it")

        assert await queue.enqueue("delivery-a", "pull_request", PAYLOAD) is True

        event = await row(db_session, "delivery-a")
        assert event.status == DeliveryStatus.PENDING
        assert event.attempts == 0
        assert event.last_error is None
        assert event.next_attempt_at is None

    async def test_the_revived_delivery_is_leased_again(self, queue: WebhookDeliveryQueue) -> None:
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
        leased = await queue.lease(limit=10, lease_for=LEASE)
        await queue.give_up(leased[0], error="gave up")
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)

        again = await queue.lease(limit=10, lease_for=LEASE)

        assert [delivery.delivery_id for delivery in again] == ["delivery-a"]

    async def test_the_body_is_taken_from_the_redelivery(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
        leased = await queue.lease(limit=10, lease_for=LEASE)
        await queue.give_up(leased[0], error="gave up")

        await queue.enqueue("delivery-a", "pull_request", {"action": "closed", "number": 7})

        assert (await row(db_session, "delivery-a")).payload["action"] == "closed"

    async def test_a_repeat_of_something_already_done_is_still_a_duplicate(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        """Only a delivery that was given up on is revived. The guard still has a job to do."""
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)
        leased = await queue.lease(limit=10, lease_for=LEASE)
        await queue.finish(leased[0], DeliveryStatus.PROCESSED)

        assert await queue.enqueue("delivery-a", "pull_request", PAYLOAD) is False
        assert (await row(db_session, "delivery-a")).status == DeliveryStatus.PROCESSED

    async def test_a_repeat_of_one_still_pending_is_still_a_duplicate(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        await queue.enqueue("delivery-a", "pull_request", PAYLOAD)

        assert await queue.enqueue("delivery-a", "pull_request", PAYLOAD) is False
        assert await count_events(db_session) == 1


class TestWhatADeliveryIsAbout:
    """A delivery id names a row. An operator needs to know which repository and which item."""

    def test_it_names_the_event_the_repository_and_the_number(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc-123",
            event_type="pull_request",
            payload={
                "action": "opened",
                "repository": {"full_name": "acme/atlas"},
                "pull_request": {"number": 42},
            },
            attempts=0,
        )

        assert delivery.subject == "pull_request.opened acme/atlas#42"

    def test_a_comment_is_named_by_the_item_it_is_on(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issue_comment",
            payload={
                "action": "created",
                "repository": {"full_name": "acme/atlas"},
                "issue": {"number": 7},
            },
            attempts=0,
        )

        assert delivery.subject == "issue_comment.created acme/atlas#7"

    def test_a_payload_with_no_item_still_names_the_repository(self) -> None:
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issues",
            payload={"action": "opened", "repository": {"full_name": "acme/atlas"}},
            attempts=0,
        )

        assert delivery.subject == "issues.opened acme/atlas"

    def test_a_payload_with_nothing_useful_says_what_it_can(self) -> None:
        delivery = Delivery(
            id=1, delivery_id="abc", event_type="ping", payload={"zen": "x"}, attempts=0
        )

        assert delivery.subject == "ping"

    def test_a_hostile_payload_does_not_break_it(self) -> None:
        """The payload is whatever arrived on the wire, so nothing here may assume a shape."""
        delivery = Delivery(
            id=1,
            delivery_id="abc",
            event_type="issues",
            payload={"repository": "not a dict", "issue": [1, 2, 3], "action": 7},
            attempts=0,
        )

        assert delivery.subject == "issues"
