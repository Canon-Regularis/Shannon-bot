from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import WebhookEvent
from shannon.discord_bot.errors import DiscordPermissionError
from shannon.domain.enums import DeliveryStatus
from shannon.github.webhooks.events import WebhookOutcome
from shannon.github.webhooks.router import EventRouter
from shannon.services.delivery.queue import WebhookDeliveryQueue
from shannon.services.delivery.worker import DeliveryWorker, WorkerSettings
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    """The real endpoint and the real worker over a registered repository."""
    async with registered_stack(db_engine, db_session, threads) as http_client:
        yield http_client


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
    response = await post(client, "issues", payloads.issue_event("opened"))

    assert response.json()["status"] == "accepted"
    assert threads.created == []


async def test_running_the_worker_then_produces_the_thread(
    client: DeliveryClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    await post(client, "issues", payloads.issue_event("opened"))

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
        await post(client, "issues", event, delivery=f"delivery-{index}")

    await client.drain()

    assert [locked for _, locked in threads.locks] == [True, False]


async def test_the_worker_takes_a_whole_batch_at_once(queue: WebhookDeliveryQueue) -> None:
    worker = build_worker(queue, Exploding(failures=0), batch_size=3)
    for index in range(5):
        await enqueue(queue, f"delivery-{index}")

    assert await worker.run_once() == 3
    assert await worker.run_once() == 2


class TestErrorsRetryingCannotFix:
    """Two hours of backoff does not grant a permission the bot was never given."""

    async def test_a_permission_error_is_given_up_on_at_once(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        async def refused(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            raise DiscordPermissionError("Discord will not let the bot create a thread")

        worker = build_worker(queue, refused)
        await enqueue(queue, "delivery-a")

        await worker.run_once()

        event = await stored(db_session, "delivery-a")
        assert event.status == DeliveryStatus.FAILED
        assert event.attempts == 1
        assert "DiscordPermissionError" in (event.last_error or "")

    async def test_it_is_not_leased_again(self, queue: WebhookDeliveryQueue) -> None:
        async def refused(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            raise DiscordPermissionError("nope")

        worker = build_worker(queue, refused, first_backoff=timedelta(seconds=-1))
        await enqueue(queue, "delivery-a")

        assert await worker.run_once() == 1
        assert await worker.run_once() == 0


class TestANoteThatArrivesTooEarly:
    """A comment can be leased in the same batch as the event that opens the item's thread."""

    async def test_the_comment_is_retried_rather_than_dropped(
        self, client: DeliveryClient, threads: FakeThreadGateway, db_session: AsyncSession
    ) -> None:
        threads.fail_next_create = True
        await post(client, "issues", payloads.issue_event("opened"), delivery="item")
        await post(client, "issue_comment", payloads.issue_comment_event(), delivery="note")

        await client.worker.run_once()

        # The item's own sync failed, so the comment had nowhere to go yet. Both wait.
        assert (await stored(db_session, "item")).status == DeliveryStatus.PENDING
        assert (await stored(db_session, "note")).status == DeliveryStatus.PENDING

    async def test_the_comment_lands_once_the_thread_exists(
        self, client: DeliveryClient, threads: FakeThreadGateway, db_session: AsyncSession
    ) -> None:
        threads.fail_next_create = True
        await post(client, "issues", payloads.issue_event("opened"), delivery="item")
        await post(client, "issue_comment", payloads.issue_comment_event(), delivery="note")
        client.worker._settings = WorkerSettings(first_backoff=timedelta(seconds=-1))

        await client.drain()

        assert (await stored(db_session, "note")).status == DeliveryStatus.PROCESSED
        assert any("commented" in content for _, content in threads.posts)

    async def test_a_note_on_something_never_tracked_is_still_ignored(
        self, client: DeliveryClient, db_session: AsyncSession
    ) -> None:
        """Only the not-yet case retries. An untracked item is not coming later."""
        payload = payloads.issue_comment_event(on=payloads.issue(id=999, number=999))
        await deliver(client, "issue_comment", payload, delivery="stranger")

        assert (await stored(db_session, "stranger")).status == DeliveryStatus.IGNORED


class TestStoppingCleanly:
    """A redeploy must not park a leased batch until its lease runs out."""

    async def test_the_rest_of_the_batch_goes_straight_back(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        handled: list[str] = []

        async def stop_after_one(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            handled.append(action)
            worker.stop()
            return WebhookOutcome.PROCESSED

        worker = build_worker(queue, stop_after_one)
        for index in range(4):
            await enqueue(queue, f"delivery-{index}")

        assert await worker.run_once() == 1

        assert len(handled) == 1
        assert (await stored(db_session, "delivery-0")).status == DeliveryStatus.PROCESSED
        for index in range(1, 4):
            event = await stored(db_session, f"delivery-{index}")
            assert event.status == DeliveryStatus.PENDING
            assert event.locked_until is None

    async def test_the_handed_back_deliveries_have_not_used_an_attempt(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        """Nothing was tried on them, so a restart must not count it against their budget."""

        async def stop_at_once(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            worker.stop()
            return WebhookOutcome.PROCESSED

        worker = build_worker(queue, stop_at_once)
        await enqueue(queue, "first")
        await enqueue(queue, "second")

        await worker.run_once()

        assert (await stored(db_session, "second")).attempts == 0

    async def test_another_worker_can_take_them_immediately(
        self, queue: WebhookDeliveryQueue
    ) -> None:
        async def stop_at_once(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            stopping.stop()
            return WebhookOutcome.PROCESSED

        stopping = build_worker(queue, stop_at_once)
        await enqueue(queue, "first")
        await enqueue(queue, "second")
        await stopping.run_once()

        fresh = build_worker(queue, Exploding(failures=0))
        assert await fresh.run_once() == 1

    async def test_run_forever_returns_once_it_is_asked_tostop(
        self, queue: WebhookDeliveryQueue
    ) -> None:
        worker = build_worker(queue, Exploding(failures=0), poll_interval=timedelta(seconds=0.01))
        worker.stop()

        await asyncio.wait_for(worker.run_forever(), timeout=2)


class TestWaitingForDiscord:
    """Logging in takes seconds, and nothing can be done about a delivery until it finishes."""

    async def test_nothing_is_leased_until_the_bot_is_ready(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        connected = asyncio.Event()
        handler = Exploding(failures=0)
        worker = build_worker(queue, handler, poll_interval=timedelta(seconds=0.01))
        await enqueue(queue, "delivery-a")

        running = asyncio.create_task(worker.run_forever(connected.wait))
        await asyncio.sleep(0.2)
        assert handler.calls == 0
        assert (await stored(db_session, "delivery-a")).status == DeliveryStatus.PENDING

        connected.set()
        await _until(lambda: handler.calls == 1)
        worker.stop()
        await running

        assert (await stored(db_session, "delivery-a")).status == DeliveryStatus.PROCESSED

    async def test_a_stop_while_still_waiting_is_obeyed_at_once(
        self, queue: WebhookDeliveryQueue
    ) -> None:
        """The readiness wait has to race `stop`.

        Waiting on the gateway alone leaves nothing to end it but the shutdown grace running
        out, so a process told to stop before Discord ever answers sits out the whole grace and
        is then killed. A slow or misconfigured gateway is when a restart is likeliest.
        """
        worker = build_worker(queue, Exploding(failures=0), poll_interval=timedelta(seconds=0.01))
        never = asyncio.Event()

        running = asyncio.create_task(worker.run_forever(never.wait))
        await asyncio.sleep(0.05)
        worker.stop()

        await asyncio.wait_for(running, timeout=1)

    async def test_a_gateway_that_died_is_still_reported(self, queue: WebhookDeliveryQueue) -> None:
        """Giving up quietly on a stop must not become giving up quietly on a failure."""

        async def never_connects() -> None:
            raise RuntimeError("the Discord bot stopped before it ever connected")

        worker = build_worker(queue, Exploding(failures=0))

        with pytest.raises(RuntimeError):
            await asyncio.wait_for(worker.run_forever(never_connects), timeout=2)

    async def test_without_a_check_it_starts_at_once(self, queue: WebhookDeliveryQueue) -> None:
        """No token means no bot to wait for, and the queue should still be worked."""
        handler = Exploding(failures=0)
        worker = build_worker(queue, handler, poll_interval=timedelta(seconds=0.01))
        await enqueue(queue, "delivery-a")

        running = asyncio.create_task(worker.run_forever())
        await _until(lambda: handler.calls == 1)
        worker.stop()
        await running


async def _until(condition, timeout: float = 10.0) -> None:
    """Wait for something the worker does on its own schedule, rather than guessing at a sleep."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


class TestPruning:
    """Bodies hold private-repository content, so the seven-day retention has to actually run."""

    async def test_the_worker_prunes_as_it_goes(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        worker = build_worker(
            queue,
            Exploding(failures=0),
            poll_interval=timedelta(seconds=0.01),
            retention=timedelta(seconds=-1),
        )
        await enqueue(queue, "old")
        await worker.run_once()
        assert (await stored(db_session, "old")).status == DeliveryStatus.PROCESSED

        running = asyncio.create_task(worker.run_forever())
        await _until(lambda: True)
        await asyncio.sleep(0.2)
        worker.stop()
        await running

        db_session.expire_all()
        assert await db_session.scalar(select(func.count()).select_from(WebhookEvent)) == 0

    async def test_a_delivery_still_waiting_is_never_pruned(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        await enqueue(queue, "waiting")

        removed = await queue.prune(keep_for=timedelta(seconds=-1))

        assert removed == 0
        assert (await stored(db_session, "waiting")).status == DeliveryStatus.PENDING


class TestWhenTheBotNeverConnects:
    """wait_until_ready waits on an event only ever set by a successful connection."""

    async def test_a_readiness_check_that_fails_stops_the_worker_loudly(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        async def never_connects() -> None:
            raise RuntimeError("the Discord bot stopped before it ever connected")

        handler = Exploding(failures=0)
        worker = build_worker(queue, handler, poll_interval=timedelta(seconds=0.01))
        await enqueue(queue, "delivery-a")

        with pytest.raises(RuntimeError, match="ever connected"):
            await worker.run_forever(never_connects)

        assert handler.calls == 0
        # Left for a process that can actually reach Discord, rather than burned through.
        event = await stored(db_session, "delivery-a")
        assert event.status == DeliveryStatus.PENDING
        assert event.attempts == 0


class TestPruningThatFails:
    async def test_a_failing_prune_is_not_retried_on_every_poll(
        self, queue: WebhookDeliveryQueue
    ) -> None:
        """The timer used to move only on success, so a broken prune ran every couple of seconds."""
        attempts = 0

        async def failing_prune(*, keep_for: timedelta) -> int:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("the prune failed")

        worker = build_worker(
            queue,
            Exploding(failures=0),
            poll_interval=timedelta(seconds=0.01),
            prune_interval=timedelta(hours=1),
        )
        worker._queue = _PruneFails(queue, failing_prune)

        running = asyncio.create_task(worker.run_forever())
        await asyncio.sleep(0.3)
        worker.stop()
        await running

        assert attempts == 1


class _PruneFails:
    """The real queue with one method swapped, so everything else behaves normally."""

    def __init__(self, queue: WebhookDeliveryQueue, prune) -> None:
        self._queue = queue
        self.prune = prune

    def __getattr__(self, name: str):
        return getattr(self._queue, name)


class TestBeingCancelledOutright:
    """What happens when the grace period runs out before the delivery in hand finishes."""

    async def test_the_rest_of_the_batch_is_handed_back(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        async def hang(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
            await asyncio.sleep(60)
            return WebhookOutcome.PROCESSED

        worker = build_worker(queue, hang)
        for index in range(4):
            await enqueue(queue, f"delivery-{index}")

        running = asyncio.create_task(worker.run_once())
        await _until(lambda: True)
        await asyncio.sleep(0.2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.2)

        # The one in hand keeps its lease and waits it out; the untouched three come straight
        # back, rather than sitting locked while the replacement process polls an empty queue.
        for index in range(1, 4):
            event = await stored(db_session, f"delivery-{index}")
            assert event.status == DeliveryStatus.PENDING, f"delivery-{index} was left locked"
            assert event.attempts == 0


class TestWhatGetsWrittenToLastError:
    """`last_error` exists to be read by a person, so it is trimmed on the way in.

    The column is Text and PostgreSQL will take any length, so nothing else enforces this. It
    had no test at all until the limit moved out of the store and into WorkerSettings.
    """

    async def test_an_enormous_message_is_trimmed(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        async def raises(action: str, payload: Any) -> WebhookOutcome:
            raise RuntimeError("x" * 10_000)

        await enqueue(queue, "huge")
        await build_worker(queue, raises).run_once()

        event = await db_session.scalar(
            select(WebhookEvent).where(WebhookEvent.github_delivery_id == "huge")
        )
        assert len(event.last_error) == WorkerSettings().error_limit

    async def test_an_ordinary_message_is_left_alone(
        self, queue: WebhookDeliveryQueue, db_session: AsyncSession
    ) -> None:
        async def raises(action: str, payload: Any) -> WebhookOutcome:
            raise RuntimeError("the gateway said no")

        await enqueue(queue, "small")
        await build_worker(queue, raises).run_once()

        event = await db_session.scalar(
            select(WebhookEvent).where(WebhookEvent.github_delivery_id == "small")
        )
        assert event.last_error == "RuntimeError: the gateway said no"
