"""Startup and shutdown, in the order they have to happen."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from shannon.config import Settings
from shannon.db.session import build_probe_engine
from shannon.runtime.liveness import ProcessLiveness
from shannon.runtime.supervision import (
    Shutdown,
    ask_the_process_to_stop,
    report_exit,
    safely,
    stop,
    why,
)
from shannon.services.delivery.worker import ReadyCheck

logger = logging.getLogger(__name__)

# How long the startup database check waits before giving up. Long enough for a cold connection
# and a first query on a loaded server, short enough that an orchestrator sees a process that
# failed to start rather than one that never answers.
STARTUP_CHECK_SECONDS = 15.0


class ProcessParts(Protocol):
    """What owning the process needs of the wiring, and nothing else.

    Container satisfies this by shape, so starting and stopping does not depend on the
    composition root or on everything it happens to hold.
    """

    engine: AsyncEngine
    worker: RunsDeliveries
    poller: PollsABoard

    async def aclose(self) -> None: ...


class RunsDeliveries(Protocol):
    """The worker as the lifespan sees it: something to start and something to ask to stop."""

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None: ...

    def stop(self) -> None: ...


class PollsABoard(Protocol):
    """The project poller as the lifespan sees it, which is the worker's shape exactly.

    A board is asked rather than delivered, because GitHub sends no project webhook for a
    personal account, so this is a second long-lived task beside the worker rather than another
    handler behind the queue.
    """

    enabled: bool

    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


class Gateway(Protocol):
    """The three things this process does to the Discord connection.

    Named here rather than importing the client, so starting and stopping the process does not
    depend on discord.py. The lifespan tests already stand a fake in its place.
    """

    async def start(self, token: str) -> None: ...

    async def wait_until_ready(self) -> None: ...

    def is_ready(self) -> bool: ...

    def gateway_is_up(self) -> bool:
        """Whether Discord can be reached right now, which is not what `is_ready` answers.

        `is_ready` reports whether the cache has ever been filled. It is set once and cleared
        only by `close`, so a connection that came up and later died still reads as ready, and
        the health check built on it could never report the one failure it was written for.
        """
        ...

    async def close(self) -> None: ...


async def require_a_working_database(engine: AsyncEngine) -> None:
    """Prove the database answers and has been migrated before the port opens.

    Building an engine connects to nothing, so without this a wrong password or a database that
    has never been migrated still reaches "startup complete" and passes a health check, while
    every delivery is accepted and then fails behind it. Failing here stops the process with
    something an operator can act on.

    Deadlined, because nothing else here is. asyncpg's sixty seconds bound the handshake and not
    the query, so a server that accepts the connection and then goes quiet, whether a primary that
    fails over once the socket is up, a black-holed route, or anything holding the table, leaves
    this waiting for as long as the kernel keeps retrying. Uvicorn opens no listening socket until
    startup returns and reads a signal only afterwards, so for all of that the process serves
    nothing, answers no health check, and cannot be asked to stop.
    """
    async with asyncio.timeout(STARTUP_CHECK_SECONDS), engine.connect() as connection:
        # Reading alembic_version proves the database answers and that migrations have been
        # applied at least once. It does not prove they are at head; doing that would mean
        # loading the Alembic environment into the running app, which is not worth it for a
        # check whose job is to catch a wrong URL or a database nobody has migrated at all.
        await connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))


def gateway_ready(bot: Gateway, bot_task: asyncio.Task) -> ReadyCheck:
    """Wait for the gateway, and give up if the bot stops trying to reach it.

    `wait_until_ready` waits on an event that is only ever set once a connection succeeds, so a
    bad token or a refused login leaves it waiting for the rest of the process's life. A worker
    parked there never leases anything: the endpoint goes on accepting deliveries, the queue
    grows, pruning never runs, and nothing says why. Failing instead stops the worker loudly and
    leaves the deliveries pending for a process that can actually reach Discord.
    """

    async def connected() -> None:
        ready = asyncio.ensure_future(bot.wait_until_ready())
        try:
            await asyncio.wait({ready, bot_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()

        if not bot_task.done():
            return

        raise RuntimeError("the Discord bot stopped before it ever connected")

    return connected


@dataclass(slots=True)
class _Running:
    """The tasks this process started, and the flag that says whether it asked them to end."""

    shutdown: Shutdown
    worker_task: asyncio.Task
    bot_task: asyncio.Task | None
    poller_task: asyncio.Task | None = None


async def _start(
    bot: Gateway,
    container: ProcessParts,
    settings: Settings,
    liveness: ProcessLiveness,
    halt: Callable[[], None],
) -> _Running:
    """Bring up the gateway and the worker, in that order.

    The worker waits for the gateway rather than racing it. The endpoint accepts deliveries from
    the moment the port is open, which is the point, but acting on one before Discord is
    connected only wastes an attempt.

    `halt` is what the two tasks that carry the point of the process do when they end without
    being asked to. The poller does not, because a process with no poller still mirrors
    everything the webhooks bring it.
    """
    shutdown = Shutdown()
    bot_task: asyncio.Task | None = None
    ready: ReadyCheck | None = None

    token = settings.discord_token.get_secret_value()
    if token:
        bot_task = asyncio.create_task(bot.start(token))
        # discord.py reconnects on its own, so this task ending at all means it gave up: a
        # refused token, a close code it cannot come back from. The worker only waits for the
        # gateway once, at the start, so a connection lost after that leaves it leasing
        # deliveries that every one of them fails.
        bot_task.add_done_callback(report_exit("Discord bot", shutdown, halt))
        ready = gateway_ready(bot, bot_task)
    else:
        # Handy for poking the webhook endpoint locally, and loud enough that nobody deploys
        # like this by accident.
        logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

    worker_task = asyncio.create_task(container.worker.run_forever(ready))
    worker_task.add_done_callback(report_exit("delivery worker", shutdown, halt))

    # Only when a board was configured. Starting a task that returns at once would have the done
    # callback report the poller as having stopped, on every boot, for everybody not using one.
    poller_task: asyncio.Task | None = None
    if container.poller.enabled:
        poller_task = asyncio.create_task(container.poller.run_forever())
        poller_task.add_done_callback(report_exit("project poller", shutdown))

    # A worker that dies takes the whole point of the process with it, and the endpoint would go
    # on answering 200 to deliveries nothing will act on. /health is what makes that visible.
    liveness.worker_task = worker_task
    liveness.bot_task = bot_task
    liveness.poller_task = poller_task
    # Asked only when there is a bot. Safe at any point in a client's life: it reads a flag the
    # client keeps from its own connect and disconnect events, and `is_ready` behind it checks
    # the sentinel before the event.
    liveness.gateway_is_ready = bot.gateway_is_up if bot_task is not None else None
    return _Running(
        shutdown=shutdown, worker_task=worker_task, bot_task=bot_task, poller_task=poller_task
    )


async def _close(
    bot: Gateway, container: ProcessParts, settings: Settings, running: _Running
) -> None:
    """Take everything down, reporting a step that fails rather than abandoning the rest."""
    # Set before anything stops, so the done callbacks can tell a task that failed from one that
    # was told to finish.
    running.shutdown.asked = True

    # Asked to stop rather than cancelled, so the delivery in hand finishes and the rest of its
    # batch goes back on the queue instead of sitting locked for the whole lease while the
    # replacement process polls an empty one.
    container.worker.stop()
    container.poller.stop()
    await safely(
        "stop the worker",
        stop(running.worker_task, grace=settings.worker_shutdown_grace_seconds),
    )
    await safely(
        "stop the project poller",
        stop(running.poller_task, grace=settings.worker_shutdown_grace_seconds),
    )
    if running.bot_task is not None:
        await safely("close the Discord client", bot.close())
        await safely("stop the bot", stop(running.bot_task))
    await safely("close the container", container.aclose())


def build_lifespan(
    bot: Gateway,
    container: ProcessParts,
    settings: Settings,
    halt: Callable[[], None] = ask_the_process_to_stop,
):
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await require_a_working_database(container.engine)
        except Exception as error:
            logger.error(
                "cannot reach the database, or it has never been migrated: %s. "
                "Check SHANNON_DATABASE_URL and run `alembic upgrade head`.",
                why(error),
            )
            raise

        probes = build_probe_engine(container.engine)
        liveness = ProcessLiveness(probes)
        app.state.liveness = liveness

        running = await _start(bot, container, settings, liveness, halt)
        try:
            yield
        finally:
            await _close(bot, container, settings, running)
            await safely("close the health probe engine", probes.dispose())

    return lifespan
