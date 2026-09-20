from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from shannon.config import Settings
from shannon.domain.enums import DeliveryStatus
from shannon.domain.errors import PermanentError
from shannon.domain.json import JsonObject
from shannon.github.webhooks.events import WebhookOutcome
from shannon.services.delivery.queue import Delivery, DeliveryQueue

logger = logging.getLogger(__name__)

ReadyCheck = Callable[[], Awaitable[None]]


class Dispatch(Protocol):
    """Handing a delivery to whatever handles that event type.

    The router satisfies this by shape; nothing here imports it.
    """

    async def dispatch(
        self,
        event: str,
        action: str | None,
        payload: JsonObject,
        arrived: int | None = None,
    ) -> WebhookOutcome: ...


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """How hard the worker tries, and how long it holds on.

    The defaults survive roughly two hours of Discord being unreachable.
    """

    poll_interval: timedelta = timedelta(seconds=2)
    batch_size: int = 10
    # Sixteen attempts is what those two hours cost; growth stops at the cap after the ninth.
    max_attempts: int = 16
    first_backoff: timedelta = timedelta(seconds=5)
    max_backoff: timedelta = timedelta(minutes=15)
    # Long enough for a whole batch of deliveries each running to the timeout: a lease that
    # expires mid-batch lets a second replica take deliveries this one is still working.
    lease: timedelta = timedelta(minutes=15)
    # How long the first batch waits on Discord before going ahead without it. Long enough for a
    # slow login, short enough that a gateway which is never coming back does not take the queue
    # with it. Deliberately not an environment knob.
    gateway_wait: timedelta = timedelta(minutes=5)
    delivery_timeout: timedelta = timedelta(seconds=60)
    retention: timedelta = timedelta(days=7)
    # A stringified handler traceback can be enormous and `last_error` is read by a person. The
    # column is Text, so this is a readability limit and not a schema one.
    error_limit: int = 2000
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
        # The exponent is clamped because `2 ** attempts` is computed before the cap applies.
        # `grown` is annotated because `2 ** n` is `Any` to a type checker: a negative exponent
        # would make it a float, which `min` below would then hand back untyped.
        grown: timedelta = self.first_backoff * 2 ** min(max(attempts, 0), 32)
        return min(grown, self.max_backoff)

    def total_backoff(self) -> timedelta:
        """How long a delivery is held before it is given up on."""
        return sum(
            (self.backoff_for(attempt) for attempt in range(self.max_attempts - 1)),
            timedelta(),
        )


class DeliveryWorker:
    """Does the work the webhook endpoint no longer does inline.

    GitHub gives an endpoint ten seconds and never redelivers, so the route writes the delivery
    down and everything slow, meaning every Discord call, happens here.
    """

    def __init__(
        self,
        queue: DeliveryQueue,
        dispatch: Dispatch,
        settings: WorkerSettings | None = None,
    ) -> None:
        self._queue = queue
        self._dispatch = dispatch
        self._settings = settings or WorkerSettings()
        self._stopping = False
        # The flag alone is not enough before the loop starts: the wait for Discord reaches no
        # check and has nothing to interrupt it, so the stop is published as something waitable.
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """Ask the worker to finish the delivery it is on and come back."""
        self._stopping = True
        self._stopped.set()

    async def run_once(self) -> int:
        """Work through one batch, returning how many deliveries were handled.

        Deliveries are handled one at a time in arrival order, so two events for the same item
        keep their order.
        """
        deliveries = await self._queue.lease(
            limit=self._settings.batch_size, lease_for=self._settings.lease
        )

        for index, delivery in enumerate(deliveries):
            if self._stopping:
                # Anything not started is handed straight back, or it sits locked for the whole
                # lease while the replacement process polls a queue that looks empty.
                await self._queue.release(deliveries[index:])
                return index
            try:
                await self._handle(delivery)
            except Exception:
                # Only the queue write recording the outcome reaches here; a database that cannot
                # take it leaves this delivery and every one behind it leased and PROCESSING
                # until the lease runs out. Released from this one inclusive: `release` moves
                # only rows still marked PROCESSING, so an outcome that did commit is left alone.
                await self._queue.release(deliveries[index:])
                raise
            except asyncio.CancelledError:
                # Cancelled mid-delivery: hand back the rest of the batch rather than let it sit
                # out the lease. Best effort, since an await inside a cancelled task returns at
                # once; if the loop closes first those rows wait out the lease instead.
                await asyncio.shield(self._queue.release(deliveries[index + 1 :]))
                raise
        return len(deliveries)

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None:
        """Work the queue until asked to stop.

        `wait_for_ready` holds the first batch back until Discord is connected: a delivery leased
        before that fails against a client with no session, spending attempts on a problem that
        fixes itself.
        """
        if wait_for_ready is not None:
            logger.info("waiting for Discord before working through the queue")
            if not await self._ready_or_stopped(wait_for_ready):
                logger.info("asked to stop before Discord ever connected")
                return

        pruned_after = 0.0
        loop = asyncio.get_running_loop()

        while not self._stopping:
            try:
                handled = await self.run_once()

                if loop.time() >= pruned_after:
                    # Rescheduled whatever happens: moving it only on success would retry a
                    # failing prune on every poll for as long as the failure lasts.
                    pruned_after = loop.time() + self._settings.prune_interval.total_seconds()
                    removed = await self._queue.prune(keep_for=self._settings.retention)
                    if removed:
                        logger.info("pruned %s finished deliveries", removed)

                # Straight back round while there is a backlog, so a burst drains at once.
                if handled < self._settings.batch_size and not self._stopping:
                    await asyncio.sleep(self._settings.poll_interval.total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bad batch must not kill the loop, or every later delivery waits for a restart.
                logger.exception("the delivery worker hit an error, carrying on")
                await asyncio.sleep(self._settings.poll_interval.total_seconds())

    async def _ready_or_stopped(self, wait_for_ready: ReadyCheck) -> bool:
        """Wait for Discord, and give up the moment a stop is asked for instead.

        Waiting on the gateway alone leaves `stop` unnoticed until the shutdown grace runs out.
        The wait is bounded because the discord.py client reconnects for ever by design: a
        gateway outage, blocked egress, or a handshake that never completes leaves
        `wait_until_ready()` unfired, and the worker then leases and prunes nothing for the life
        of the process while `/health` still reports it alive. Returns whether to carry on; an
        error from the wait is raised, since a bot that stopped before connecting is a failure.
        """
        ready = asyncio.ensure_future(wait_for_ready())
        stopped = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait(
                {ready, stopped},
                timeout=self._settings.gateway_wait.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not ready.done() and not stopped.done():
                logger.error(
                    "Discord has not connected after %ss, working the queue without it",
                    int(self._settings.gateway_wait.total_seconds()),
                )
                return True
        finally:
            stopped.cancel()
            if not ready.done():
                ready.cancel()

        # Checked before the flag, so a gateway that failed is reported rather than read as an
        # ordinary stop when both finish together.
        if ready.done() and not ready.cancelled() and (failed := ready.exception()) is not None:
            raise failed
        return not self._stopping

    async def _handle(self, delivery: Delivery) -> None:
        try:
            outcome = await asyncio.wait_for(
                self._dispatch.dispatch(
                    delivery.event_type,
                    delivery.action,
                    delivery.payload,
                    # Arrival order, the only thing separating two deliveries GitHub stamped
                    # with the same second.
                    delivery.id,
                ),
                timeout=self._settings.delivery_timeout.total_seconds(),
            )
        except asyncio.CancelledError:
            raise
        except PermanentError as error:
            # A missing permission or a channel that cannot hold threads does not heal on its own.
            logger.error(
                "delivery %s (%s) cannot be handled: %s",
                delivery.delivery_id,
                delivery.subject,
                error.message,
            )
            await self._queue.give_up(delivery, error=self._reason(error))
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

    def _reason(self, error: Exception) -> str:
        return f"{type(error).__name__}: {error}"[: self._settings.error_limit]

    async def _reschedule(self, delivery: Delivery, error: Exception) -> None:
        attempts = delivery.attempts + 1
        reason = self._reason(error)

        if attempts >= self._settings.max_attempts:
            logger.error(
                "giving up on delivery %s (%s) after %s attempts: %s",
                delivery.delivery_id,
                delivery.subject,
                attempts,
                reason,
            )
            await self._queue.give_up(delivery, error=reason)
            return

        delay = self._settings.backoff_for(delivery.attempts)
        logger.warning(
            "delivery %s (%s) failed (attempt %s), retrying in %ss: %s",
            delivery.delivery_id,
            delivery.subject,
            attempts,
            int(delay.total_seconds()),
            reason,
        )
        await self._queue.retry_later(delivery, error=reason, delay=delay)
