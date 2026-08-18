"""What the process can honestly say about itself to /health."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessLiveness:
    """What /health reports, kept where the things it reports on actually live."""

    engine: AsyncEngine
    worker_task: asyncio.Task | None = None
    # None means no token was configured, which is the deliberate no-bot mode. Otherwise a
    # finished task means the gateway has gone.
    bot_task: asyncio.Task | None = None
    # How long a probe is reused. The route is public, and a connection per request would let a
    # flood exhaust the pool the worker runs on.
    probe_every: float = 5.0
    # A host that accepts the connection and then goes quiet would otherwise park the probe for
    # as long as the kernel allows, with every health check queued behind it.
    probe_timeout: float = 5.0
    _probed_at: float = 0.0
    _reachable: bool = False
    # One probe at a time, or a burst all misses the cache together and opens a connection each.
    _probing: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _answer_is_fresh(self) -> bool:
        return bool(self._probed_at) and time.monotonic() - self._probed_at < self.probe_every

    async def database_reachable(self) -> bool:
        if self._answer_is_fresh():
            return self._reachable

        async with self._probing:
            # Whoever held the lock may have just answered this.
            if self._answer_is_fresh():
                return self._reachable

            try:
                async with asyncio.timeout(self.probe_timeout), self.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception as error:
                logger.warning("the database is not reachable: %s", error)
                self._reachable = False
            else:
                self._reachable = True

            # Stamped once there is an answer, never on the way in. Stamping first marks the
            # result fresh while it is still being worked out, so callers arriving in that window
            # read `_reachable` before anything has set it: on a first probe, its initial False.
            self._probed_at = time.monotonic()
            return self._reachable

    def worker_running(self) -> bool:
        return self.worker_task is not None and not self.worker_task.done()

    def bot_connected(self) -> bool:
        """Whether the gateway is still there, if this deployment has one at all.

        The worker only waits for the bot once, before its first batch, so a gateway that dies
        after connecting leaves the worker running and leasing while every Discord call fails.
        Reporting only the worker would call that healthy, which is exactly the case this
        endpoint was added for.
        """
        return self.bot_task is None or not self.bot_task.done()
