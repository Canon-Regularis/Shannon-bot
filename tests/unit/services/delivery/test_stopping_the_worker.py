"""How long the worker takes to end when it is told to, with no database behind it.

The queue here answers without ever giving way to another task. So once a test sees that the
worker has asked it for a batch, the worker has already gone on to the wait for the next one, and
no sleep is needed to let it get there.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from shannon.domain.enums import DeliveryStatus
from shannon.domain.json import JsonObject
from shannon.github.webhooks.events import WebhookOutcome
from shannon.services.delivery.queue import Delivery
from shannon.services.delivery.worker import DeliveryWorker, WorkerSettings
from tests.support.waiting import until

# Far longer than the process is ever given to stop in, and never actually waited out.
NEVER = timedelta(hours=1)
# How long a worker that a stop has woken may take to end. Only a worker still sitting out the
# interval comes near it.
PROMPTLY = 2.0


def a_delivery(number: int) -> Delivery:
    return Delivery(
        id=number,
        delivery_id=f"delivery-{number}",
        event_type="issues",
        payload={"action": "opened"},
        attempts=0,
    )


class Queue:
    """Hands out one batch, and then nothing. Or fails to, once, and never answers again."""

    def __init__(self, *batch: Delivery, fails: bool = False) -> None:
        self._batch = batch
        self._fails = fails
        self.leases = 0
        self.releasing = asyncio.Event()
        self.may_release = asyncio.Event()
        self.released: list[Delivery] = []

    async def lease(self, *, limit: int, lease_for: timedelta) -> Sequence[Delivery]:
        self.leases += 1
        if self._fails:
            # Parked after the first, so a worker that does not wait is counted rather than left
            # to spin without ever giving the test a turn.
            if self.leases > 1:
                await asyncio.Event().wait()
            raise RuntimeError("the database is down")
        batch, self._batch = self._batch, ()
        return batch

    async def release(self, deliveries: Sequence[Delivery]) -> None:
        self.releasing.set()
        await self.may_release.wait()
        self.released.extend(deliveries)

    async def finish(self, delivery: Delivery, status: DeliveryStatus) -> None: ...

    async def retry_later(self, delivery: Delivery, *, error: str, delay: timedelta) -> None: ...

    async def give_up(self, delivery: Delivery, *, error: str) -> None: ...

    async def prune(self, *, keep_for: timedelta) -> int:
        return 0


class Hangs:
    """A handler that never answers."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def dispatch(
        self, event: str, action: str | None, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("nothing sets that event")


def build(queue: Queue, dispatch: Hangs | None = None) -> DeliveryWorker:
    return DeliveryWorker(queue, dispatch or Hangs(), WorkerSettings(poll_interval=NEVER))


@asynccontextmanager
async def running(worker: DeliveryWorker) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(worker.run_forever())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestAStopWhileWaitingForTheNextBatch:
    """The wait between batches is nearly all the worker ever does, so it is where a stop lands.

    Sleeping it out spends the shutdown grace on nothing, and an interval longer than the grace
    had an idle worker reported as one that would not stop, and cancelled (#272).
    """

    async def test_an_idle_worker_ends_without_waiting_out_the_interval(self) -> None:
        queue = Queue()
        worker = build(queue)

        async with running(worker) as task:
            await until(lambda: queue.leases == 1)
            worker.stop()
            await asyncio.wait_for(task, timeout=PROMPTLY)

        assert queue.leases == 1, "it took another batch after being asked to stop"

    async def test_a_worker_whose_batch_failed_ends_without_waiting_out_the_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        queue = Queue(fails=True)
        worker = build(queue)

        async with running(worker) as task:
            await until(lambda: "the delivery worker hit an error, carrying on" in caplog.text)
            worker.stop()
            await asyncio.wait_for(task, timeout=PROMPTLY)

        assert queue.leases == 1, "it tried again after being asked to stop"

    async def test_a_worker_nobody_stopped_still_waits_after_a_failure(self) -> None:
        """The other half. A wait that does not hold is a hot loop against a broken database."""
        queue = Queue(fails=True)

        async with running(build(queue)) as task:
            await until(lambda: queue.leases >= 1)
            assert not task.done()

        assert queue.leases == 1, "it tried again without waiting out the interval"

    async def test_an_interval_that_runs_out_is_not_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The wait sits inside the block that logs a failed batch, so a timeout let out of it
        would be logged as one on every idle poll."""
        queue = Queue()
        worker = DeliveryWorker(
            queue, Hangs(), WorkerSettings(poll_interval=timedelta(milliseconds=1))
        )

        async with running(worker) as task:
            await until(lambda: queue.leases >= 3)
            worker.stop()
            await asyncio.wait_for(task, timeout=PROMPTLY)

        assert "the delivery worker hit an error" not in caplog.text


class TestHandingBackOnCancellation:
    async def test_the_hand_back_is_waited_for_and_a_second_cancel_does_not_undo_it(self) -> None:
        """A cancelled task is cancelled once, so what it awaits afterwards runs as usual. The
        shield is for a second cancel: that one ends the worker, and not the hand-back."""
        left_in_hand, *untouched = (a_delivery(number) for number in (1, 2, 3))
        queue = Queue(left_in_hand, *untouched)
        handler = Hangs()

        async with running(build(queue, handler)) as task:
            await asyncio.wait_for(handler.started.wait(), timeout=PROMPTLY)
            task.cancel()
            await asyncio.wait_for(queue.releasing.wait(), timeout=PROMPTLY)
            done, _ = await asyncio.wait({task}, timeout=0.1)
            assert not done, "the worker ended before its hand-back did"

            task.cancel()
            await asyncio.wait({task}, timeout=PROMPTLY)
            assert task.cancelled()
            assert queue.released == []

            queue.may_release.set()
            await until(lambda: queue.released == untouched)
