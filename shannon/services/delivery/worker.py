from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from shannon.config import Settings
from shannon.domain.enums import DeliveryStatus
from shannon.domain.errors import PermanentError
from shannon.github.webhooks.events import WebhookOutcome
from shannon.services.delivery.queue import Delivery, DeliveryQueue

logger = logging.getLogger(__name__)

ReadyCheck = Callable[[], Awaitable[None]]


class Dispatch(Protocol):
    """Handing a delivery to whatever handles that event type.

    Declared here rather than imported, so the worker names what it needs instead of depending
    on the router that happens to provide it. The router satisfies this by shape.
    """

    async def dispatch(
        self, event: str, action: str | None, payload: Mapping[str, Any]
    ) -> WebhookOutcome: ...


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
    # Long enough to cover a whole batch at its worst, which is every delivery in it running to
    # the timeout, and short enough that a killed worker's rows come back reasonably soon. A
    # lease that expires while the batch is still being worked would let a second replica take
    # deliveries this one is in the middle of.
    lease: timedelta = timedelta(minutes=15)
    # How long the first batch waits on Discord before going ahead without it. Long enough to
    # cover an ordinary login, which is seconds, and a slow one; short enough that a gateway that
    # is never coming back does not take the queue with it. Not an environment knob: nothing
    # about a deployment makes a different number right, and the failure it guards against is one
    # nobody knew they had.
    gateway_wait: timedelta = timedelta(minutes=5)
    delivery_timeout: timedelta = timedelta(seconds=60)
    retention: timedelta = timedelta(days=7)
    # A stack trace stringified from a handler can be enormous, and `last_error` exists to be
    # read by a person. The column is Text, so this is a readability limit and not a schema one.
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
        queue: DeliveryQueue,
        dispatch: Dispatch,
        settings: WorkerSettings | None = None,
    ) -> None:
        self._queue = queue
        self._dispatch = dispatch
        self._settings = settings or WorkerSettings()
        self._stopping = False
        # The flag is enough for the loop, which reaches a check every couple of seconds. It is
        # not enough before the loop starts, where the wait for Discord has no such check and
        # nothing to interrupt it, so the stop is published as something waitable too.
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """Ask the worker to finish the delivery it is on and come back."""
        self._stopping = True
        self._stopped.set()

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
            try:
                await self._handle(delivery)
            except Exception:
                # Everything a handler can raise is already dealt with inside `_handle`. What
                # reaches here is the queue write that records the outcome, and a database that
                # cannot take it left this delivery and every one behind it leased and marked
                # PROCESSING. Nothing else would touch them until the lease ran out a quarter of
                # an hour later, and the loop would meanwhile poll a queue that looked empty.
                #
                # Handed back from this one inclusive: whether its outcome landed is exactly what
                # is not known, and `release` only moves rows still marked PROCESSING, so a write
                # that did commit is left alone and one that did not comes back for another go.
                #
                # Re-raised rather than swallowed. The error is not this loop's to interpret and
                # `run_forever` is where the decision to carry on lives, which is also what makes
                # it visible to a caller running one batch at a time.
                await self._queue.release(deliveries[index:])
                raise
            except asyncio.CancelledError:
                # The grace period ran out mid-delivery. The rest of the batch was never touched,
                # so hand it back instead of letting it sit out the lease. Best effort: awaiting
                # from inside a cancelled task returns at once, so this starts the release
                # without seeing it finish. It usually lands, since shutdown is still waiting on
                # this task; if the loop closes first those rows wait out their lease, which is
                # where they would have been anyway. The cooperative stop above is the usual path.
                await asyncio.shield(self._queue.release(deliveries[index + 1 :]))
                raise
        return len(deliveries)

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None:
        """Work the queue until asked to stop.

        `wait_for_ready` holds the first batch back until Discord is connected. Logging in and
        connecting takes seconds, and every delivery leased before that fails against a client
        with no session, which spends attempts on a problem that fixes itself.
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
                    # Rescheduled whatever happens. Moving it only on success would have a
                    # failing prune tried again on every poll, which is every couple of
                    # seconds, for as long as the reason it failed lasts.
                    pruned_after = loop.time() + self._settings.prune_interval.total_seconds()
                    removed = await self._queue.prune(keep_for=self._settings.retention)
                    if removed:
                        logger.info("pruned %s finished deliveries", removed)

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

    async def _ready_or_stopped(self, wait_for_ready: ReadyCheck) -> bool:
        """Wait for Discord, and give up the moment a stop is asked for instead.

        Waiting on the gateway alone leaves `stop` unnoticed until the shutdown grace runs out
        and something cancels this, so a process asked to stop before Discord ever answered sits
        out the whole grace period and is then killed.

        Bounded, because the wait it replaces had no end. discord.py's client reconnects for ever
        by design: a gateway outage, blocked egress, or a handshake that never completes leaves
        `start()` running and `wait_until_ready()` unfired, with nothing here to notice. The
        worker then sat in this call for the life of the process, never leasing a delivery and
        never pruning one, while the task it belongs to was alive and `/health` said so.

        Giving up and working anyway is the better of the two. Every delivery then fails against
        a client with no session, which is a retryable gateway error the queue already backs off
        and eventually reports in `last_error`. A queue draining slowly with a visible reason beats
        one that never moves and says nothing.

        Returns whether Discord connected. An error from the wait is still raised: a bot that
        stopped before connecting is a real failure and the caller reports it.
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

        # Checked before the flag, so a gateway that failed is reported rather than being read
        # as an ordinary stop when both finish together.
        if ready.done() and not ready.cancelled() and ready.exception() is not None:
            raise ready.exception()
        return not self._stopping

    async def _handle(self, delivery: Delivery) -> None:
        try:
            outcome = await asyncio.wait_for(
                self._dispatch.dispatch(delivery.event_type, delivery.action, delivery.payload),
                timeout=self._settings.delivery_timeout.total_seconds(),
            )
        except asyncio.CancelledError:
            raise
        except PermanentError as error:
            # A missing permission or a channel that cannot hold threads does not heal on its
            # own, so retrying for two hours only delays the log line that says so.
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
        """What gets written to `last_error`, trimmed where it is written rather than where it
        is stored, so the store carries no policy of its own."""
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
