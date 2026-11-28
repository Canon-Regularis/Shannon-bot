"""What the process can honestly say about itself to /health."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import TextClause, text

from shannon.runtime.supervision import ProcessTasks, why

logger = logging.getLogger(__name__)


class ProbeConnection(Protocol):
    """One connection, as far as asking it whether it answers."""

    async def execute(self, statement: TextClause, /) -> object: ...


class OpensProbeConnections(Protocol):
    """The probe engine, and nothing of it but the opening of a connection."""

    def connect(self) -> AbstractAsyncContextManager[ProbeConnection]: ...


@dataclass(slots=True)
class ProcessLiveness:
    """What /health reports, kept where the things it reports on actually live.

    Built once everything it reports on exists, so there is no half-wired one to answer from.
    """

    # The probe's own engine and never the pooled one: a probe out of time is cancelled, and what
    # that does to a pooled connection is in `build_probe_engine`.
    engine: OpensProbeConnections
    tasks: ProcessTasks
    # Whether Discord can be reached now, which the bot's task being alive does not say.
    gateway_is_up: Callable[[], bool]
    # How long a probe is reused. The route is public and each probe opens a connection of its
    # own, so without this a flood would open one after another, with every real check queued
    # behind it.
    probe_every: float = 5.0
    # A host that accepts the connection and then goes quiet would otherwise park the probe for
    # as long as the kernel allows, with every health check queued behind it.
    probe_timeout: float = 5.0
    clock: Callable[[], float] = time.monotonic
    # None until there has been a probe: any number is a time the clock could show.
    _probed_at: float | None = field(default=None, init=False)
    _reachable: bool = field(default=False, init=False)
    # One probe at a time, or a burst all misses the cache together and opens a connection each.
    _probing: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def _answer_is_fresh(self) -> bool:
        return self._probed_at is not None and self.clock() - self._probed_at < self.probe_every

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
                logger.warning("the database is not reachable: %s", why(error))
                self._reachable = False
            else:
                self._reachable = True

            # Stamped once there is an answer, never on the way in. Stamping first would mark
            # the result fresh while it is unknown, so callers read `_reachable`'s initial False.
            self._probed_at = self.clock()
            return self._reachable

    def worker_running(self) -> bool:
        """Whether the worker's task has not ended.

        It does not say the task is getting anywhere: a queue call that hangs, or a loop that fails
        the same way on every turn, reads as running. So do the three below (#272).
        """
        return not self.tasks.worker_task.done()

    def poller_running(self) -> bool:
        """Whether boards are still being read.

        Reported without counting towards the verdict: the poller has no halt wired to it, like the
        flusher and the reminder sender, so it dies quietly, but the process works on. True where
        this process does not poll.
        """
        return self.tasks.poller_task is None or not self.tasks.poller_task.done()

    def flusher_running(self) -> bool:
        """Whether captured conversations are still being published.

        Reported without counting towards the verdict, like the poller: nothing halts the process
        when this dies, so captured words pile up in the table until a working flusher runs.
        """
        return not self.tasks.flusher_task.done()

    def reminders_running(self) -> bool:
        """Whether reminders are still being sent. Issue #229.

        Reported without counting towards the verdict, like the flusher: nothing halts the process
        when this dies, and a reminder not sent waits in the table until a working sender runs.
        True where there is no gateway to send one to.
        """
        return self.tasks.reminders_task is None or not self.tasks.reminders_task.done()

    def bot_connected(self) -> bool:
        """Whether the gateway is still there, if this deployment has one at all.

        The worker waits for the bot only once, so a gateway dying later leaves it leasing while
        Discord calls fail; discord.py reconnects for ever, so `start()` runs on unconnected.
        """
        if self.tasks.bot_task is None:
            return True
        return not self.tasks.bot_task.done() and self.gateway_is_up()
