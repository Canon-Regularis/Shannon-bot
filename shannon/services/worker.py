from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from shannon.config import Settings
from shannon.domain.enums import DeliveryStatus
from shannon.domain.errors import PermanentError
from shannon.github.webhooks.events import EventRouter, WebhookOutcome
from shannon.services.delivery_queue import Delivery, WebhookDeliveryQueue

logger = logging.getLogger(__name__)

ReadyCheck = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """How hard the worker tries, and how long it holds on.

    The defaults survive roughly two hours of Discord being unreachable, which covers an outage
    without holding a delivery so long that acting on it would be strange. `total_backoff`
    computes that figure rather than restating it.
    """

    poll_interval: timedelta = timedelta(seconds=2)
    batch_size: int = 10
    # Sixteen attempts is what the two hours actually costs. Growth stops at the cap after nine
    # of them, so the first nine are worth half an hour between them and the rest are flat.
    max_attempts: int = 16
    first_backoff: timedelta = timedelta(seconds=5)
    max_backoff: timedelta = timedelta(minutes=15)
    # Longer than any single delivery should take, short enough that a killed worker's rows
    # come back reasonably soon.
    lease: timedelta = timedelta(minutes=5)
    delivery_timeout: timedelta = timedelta(seconds=60)
    retention: timedelta = timedelta(days=7)
    prune_interval: timedelta = timedelta(hours=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerSettings:
        return cls(
            poll_interval=timedelta(seconds=settings.worker_poll_seconds),
            batch_size=settings.worker_batch_size,
            max_attempts=settings.worker_max_attempts,
            max_backoff=timedelta(seconds=settings.worker_max_backoff_seconds),
            lease=timedelta(seconds=settings.worker_lease_seconds),
            delivery_timeout=timedelta(seconds=settings.worker_delivery_timeout_seconds),
            retention=timedelta(days=settings.delivery_retention_days),
        )

    def backoff_for(self, attempts: int) -> timedelta:
        """Double the wait each time, up to the cap."""
        # 2 ** a large number is a real number here, so the cap is applied to a value that is
        # cheap to compute rather than one that grows without bound.
        grown = self.first_backoff * 2 ** min(max(attempts, 0), 32)
        return min(grown, self.max_backoff)

    def total_backoff(self) -> timedelta:
        """How long a delivery is held before it is given up on.

        Several comments quote this figure. Having it computed from the settings rather than
        written down again means they cannot drift apart from what the worker really does.
        """
        return sum(
            (self.backoff_for(attempt) for attempt in range(self.max_attempts - 1)),
            timedelta(),
        )


class DeliveryWorker:
    """Does the work the webhook endpoint no longer does inline.

    GitHub gives an endpoint ten seconds and never redelivers, so the route writes the delivery
    down and answers. Everything slow, meaning every Discord call, happens here where taking
    longer costs nothing and failing means trying again.
    """

    def __init__(
        self,
        queue: WebhookDeliveryQueue,
        router: EventRouter,
        settings: WorkerSettings | None = None,
    ) -> None:
        self._queue = queue
        self._router = router
        self._settings = settings or WorkerSettings()
        self._stopping = False

    def stop(self) -> None:
        """Ask the worker to finish the delivery it is on and come back."""
        self._stopping = True

    async def run_once(self) -> int:
        """Work through one batch, returning how many deliveries were handled.

        Deliveries are taken in the order they arrived and handled one at a time, so two events
        for the same item keep their order. A repository's events are not a stream, and
        preserving order is worth more here than working through them at once.
        """
        deliveries = await self._queue.lease(
            limit=self._settings.batch_size, lease_for=self._settings.lease
        )

        for index, delivery in enumerate(deliveries):
            if self._stopping:
                # Shutting down. Anything not started is handed straight back, or it would sit
                # locked for the whole lease while the replacement process polls an empty queue.
                await self._queue.release(deliveries[index:])
                return index
            await self._handle(delivery)
        return len(deliveries)

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None:
        """Work the queue until asked to stop.

        `wait_for_ready` holds the first batch back until Discord is connected. Logging in and
        connecting takes seconds, and every delivery leased before that fails against a client
        with no session, which spends attempts on a problem that fixes itself.
        """
        if wait_for_ready is not None:
            logger.info("waiting for Discord before working through the queue")
            await wait_for_ready()

        pruned_after = 0.0
        loop = asyncio.get_running_loop()

        while not self._stopping:
            try:
                handled = await self.run_once()

                if loop.time() >= pruned_after:
                    removed = await self._queue.prune(keep_for=self._settings.retention)
                    if removed:
                        logger.info("pruned %s finished deliveries", removed)
                    pruned_after = loop.time() + self._settings.prune_interval.total_seconds()

                # Straight back round while there is a backlog, so a burst drains at once
                # rather than one batch per tick.
                if handled < self._settings.batch_size and not self._stopping:
                    await asyncio.sleep(self._settings.poll_interval.total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop itself must not die on a bad batch, or every later delivery waits
                # for a restart.
                logger.exception("the delivery worker hit an error, carrying on")
                await asyncio.sleep(self._settings.poll_interval.total_seconds())

    async def _handle(self, delivery: Delivery) -> None:
        try:
            outcome = await asyncio.wait_for(
                self._router.dispatch(delivery.event_type, delivery.action, delivery.payload),
                timeout=self._settings.delivery_timeout.total_seconds(),
            )
        except asyncio.CancelledError:
            raise
        except PermanentError as error:
            # A missing permission or a channel that cannot hold threads does not heal on its
            # own, so retrying for two hours only delays the log line that says so.
            logger.error("delivery %s cannot be handled: %s", delivery.delivery_id, error.message)
            await self._queue.give_up(delivery, error=f"{type(error).__name__}: {error}")
            return
        except Exception as error:
            await self._reschedule(delivery, error)
            return

        await self._queue.finish(
            delivery,
            DeliveryStatus.PROCESSED
            if outcome is WebhookOutcome.PROCESSED
            else DeliveryStatus.IGNORED,
        )

    async def _reschedule(self, delivery: Delivery, error: Exception) -> None:
        attempts = delivery.attempts + 1
        reason = f"{type(error).__name__}: {error}"

        if attempts >= self._settings.max_attempts:
            logger.error(
                "giving up on delivery %s after %s attempts: %s",
                delivery.delivery_id,
                attempts,
                reason,
            )
            await self._queue.give_up(delivery, error=reason)
            return

        delay = self._settings.backoff_for(delivery.attempts)
        logger.warning(
            "delivery %s failed (attempt %s), retrying in %ss: %s",
            delivery.delivery_id,
            attempts,
            int(delay.total_seconds()),
            reason,
        )
        await self._queue.retry_later(delivery, error=reason, delay=delay)
