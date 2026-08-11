from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import WebhookEvent
from shannon.domain.enums import DeliveryStatus, ObjectType
from shannon.github.webhooks.events import EventRouter, WebhookOutcome
from shannon.services.delivery_queue import WebhookDeliveryQueue
from shannon.services.worker import DeliveryWorker, WorkerSettings
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import map_channel, register_repository
from tests.support.stack import DeliveryClient, build_http_client, build_stack, deliver, send

pytestmark = pytest.mark.integration


@pytest.fixture
async def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest.fixture
async def client(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    """The real endpoint and the real worker over a registered repository."""
    repository = await register_repository(db_session)
    await map_channel(db_session, repository, ObjectType.ISSUE, channel_id=98)
    async with build_http_client(build_stack(db_engine, threads=threads)) as http_client:
        yield http_client


@pytest.fixture
def queue(db_sessionmaker: async_sessionmaker) -> WebhookDeliveryQueue:
    return WebhookDeliveryQueue(db_sessionmaker)


async def stored(session: AsyncSession, delivery_id: str) -> WebhookEvent:
    session.expire_all()
    event = await session.scalar(
        select(WebhookEvent).where(WebhookEvent.github_delivery_id == delivery_id)
    )
    assert event is not None
    return event


class Exploding:
    """A handler that fails a set number of times before working."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    async def __call__(self, action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("discord is down")
        return WebhookOutcome.PROCESSED


def build_worker(queue: WebhookDeliveryQueue, handler: Any, **overrides: Any) -> DeliveryWorker:
    router = EventRouter()
    router.register("issues", handler)
    return DeliveryWorker(queue, router, WorkerSettings(**overrides))


async def enqueue(queue: WebhookDeliveryQueue, delivery_id: str, action: str = "opened") -> None:
    await queue.enqueue(delivery_id, "issues", payloads.issue_event(action))


async def test_a_delivery_is_answered_without_discord_being_touched(
    client: DeliveryClient, threads: FakeThreadGateway
) -> None:
    """The endpoint's whole job now: write it down, answer, touch nothing slow."""
    response = await send(client, "issues", payloads.issue_event("opened"))

    assert response.json()["status"] == "accepted"
    assert threads.created == []


async def test_running_the_worker_then_produces_the_thread(
    client: DeliveryClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    await send(client, "issues", payloads.issue_event("opened"))

    assert await client.worker.run_once() == 1

    assert len(threads.created) == 1
    assert (await stored(db_session, "delivery-1")).status == DeliveryStatus.PROCESSED


async def test_an_empty_queue_gives_the_worker_nothing_to_do(client: DeliveryClient) -> None:
    assert await client.worker.run_once() == 0


async def test_a_delivery_the_handler_ignores_is_recorded_as_ignored(
    client: DeliveryClient, db_session: AsyncSession
) -> None:
    """The payload parses fine but nothing here tracks that repository, so there was no work."""
    payload = payloads.issue_event("opened")
    payload["repository"]["id"] = 999999
    await deliver(client, "issues", payload, delivery="stranger")

    assert (await stored(db_session, "stranger")).status == DeliveryStatus.IGNORED


async def test_a_failing_delivery_goes_back_on_the_queue(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    worker = build_worker(queue, Exploding(failures=1))
    await enqueue(queue, "delivery-a")

    await worker.run_once()

    event = await stored(db_session, "delivery-a")
    assert event.status == DeliveryStatus.PENDING
    assert event.attempts == 1
    assert event.next_attempt_at is not None
    assert "discord is down" in (event.last_error or "")


async def test_a_retry_after_a_failure_succeeds(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    handler = Exploding(failures=1)
    worker = build_worker(queue, handler, first_backoff=timedelta(seconds=-1))
    await enqueue(queue, "delivery-a")

    await worker.run_once()
    await worker.run_once()

    assert handler.calls == 2
    event = await stored(db_session, "delivery-a")
    assert event.status == DeliveryStatus.PROCESSED
    assert event.last_error is None


async def test_a_delivery_waiting_on_its_backoff_is_left_alone(
    queue: WebhookDeliveryQueue,
) -> None:
    handler = Exploding(failures=1)
    worker = build_worker(queue, handler, first_backoff=timedelta(hours=1))
    await enqueue(queue, "delivery-a")

    await worker.run_once()

    assert await worker.run_once() == 0
    assert handler.calls == 1


async def test_attempts_running_out_gives_up_with_the_reason_recorded(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    handler = Exploding(failures=99)
    worker = build_worker(queue, handler, max_attempts=3, first_backoff=timedelta(seconds=-1))
    await enqueue(queue, "delivery-a")

    for _ in range(5):
        await worker.run_once()

    assert handler.calls == 3
    event = await stored(db_session, "delivery-a")
    assert event.status == DeliveryStatus.FAILED
    assert event.attempts == 3
    assert "discord is down" in (event.last_error or "")


async def test_one_bad_delivery_does_not_hold_up_the_rest(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    handler = Exploding(failures=1)
    worker = build_worker(queue, handler)
    await enqueue(queue, "bad")
    await enqueue(queue, "good")

    await worker.run_once()

    assert handler.calls == 2
    assert (await stored(db_session, "bad")).status == DeliveryStatus.PENDING
    assert (await stored(db_session, "good")).status == DeliveryStatus.PROCESSED


async def test_a_delivery_that_never_returns_is_not_allowed_to_wedge_the_queue(
    queue: WebhookDeliveryQueue, db_session: AsyncSession
) -> None:
    """discord.py sleeps through a rate limit rather than failing, so a handler can just hang."""

    async def hang(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        await asyncio.sleep(60)
        return WebhookOutcome.PROCESSED

    worker = build_worker(queue, hang, delivery_timeout=timedelta(seconds=0.01))
    await enqueue(queue, "delivery-a")

    await worker.run_once()

    event = await stored(db_session, "delivery-a")
    assert event.status == DeliveryStatus.PENDING
    assert event.attempts == 1


async def test_deliveries_for_one_item_are_handled_in_order(
    client: DeliveryClient, threads: FakeThreadGateway
) -> None:
    """A close arriving after a reopen must not be applied last, or the thread ends up wrong."""
    events = [
        payloads.issue_event("opened"),
        payloads.issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z"),
        payloads.issue_event("reopened", state="open", closed_at=None),
    ]
    for index, event in enumerate(events):
        await send(client, "issues", event, delivery=f"delivery-{index}")

    await client.drain()

    assert [locked for _, locked in threads.locks] == [True, False]


async def test_the_worker_takes_a_whole_batch_at_once(queue: WebhookDeliveryQueue) -> None:
    worker = build_worker(queue, Exploding(failures=0), batch_size=3)
    for index in range(5):
        await enqueue(queue, f"delivery-{index}")

    assert await worker.run_once() == 3
    assert await worker.run_once() == 2


async def test_backoff_doubles_up_to_the_cap() -> None:
    settings = WorkerSettings(first_backoff=timedelta(seconds=5), max_backoff=timedelta(minutes=1))

    waits = [settings.backoff_for(attempts).total_seconds() for attempts in range(6)]

    assert waits == [5, 10, 20, 40, 60, 60]
