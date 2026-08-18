"""Startup and shutdown, in the order they have to happen."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from shannon.config import Settings
from shannon.container import Container
from shannon.discord_bot.client import ShannonBot
from shannon.runtime.liveness import ProcessLiveness
from shannon.runtime.supervision import Shutdown, report_exit, safely, stop
from shannon.services.delivery.worker import ReadyCheck

logger = logging.getLogger(__name__)


async def require_a_working_database(engine: AsyncEngine) -> None:
    """Prove the database answers and has been migrated before the port opens.

    Building an engine connects to nothing, so without this a wrong password or a database that
    has never been migrated still reaches "startup complete" and passes a health check, while
    every delivery is accepted and then fails behind it. Failing here stops the process with
    something an operator can act on.
    """
    async with engine.connect() as connection:
        # Reading alembic_version proves the database answers and that migrations have been
        # applied at least once. It does not prove they are at head; doing that would mean
        # loading the Alembic environment into the running app, which is not worth it for a
        # check whose job is to catch a wrong URL or a database nobody has migrated at all.
        await connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))


def gateway_ready(bot: ShannonBot, bot_task: asyncio.Task) -> ReadyCheck:
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


async def _start(
    bot: ShannonBot, container: Container, settings: Settings, liveness: ProcessLiveness
) -> _Running:
    """Bring up the gateway and the worker, in that order.

    The worker waits for the gateway rather than racing it. The endpoint accepts deliveries from
    the moment the port is open, which is the point, but acting on one before Discord is
    connected only wastes an attempt.
    """
    shutdown = Shutdown()
    bot_task: asyncio.Task | None = None
    ready: ReadyCheck | None = None

    token = settings.discord_token.get_secret_value()
    if token:
        bot_task = asyncio.create_task(bot.start(token))
        bot_task.add_done_callback(report_exit("Discord bot", shutdown))
        ready = gateway_ready(bot, bot_task)
    else:
        # Handy for poking the webhook endpoint locally, and loud enough that nobody deploys
        # like this by accident.
        logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

    worker_task = asyncio.create_task(container.worker.run_forever(ready))
    worker_task.add_done_callback(report_exit("delivery worker", shutdown))

    # A worker that dies takes the whole point of the process with it, and the endpoint would go
    # on answering 200 to deliveries nothing will act on. /health is what makes that visible.
    liveness.worker_task = worker_task
    liveness.bot_task = bot_task
    return _Running(shutdown=shutdown, worker_task=worker_task, bot_task=bot_task)


async def _close(
    bot: ShannonBot, container: Container, settings: Settings, running: _Running
) -> None:
    """Take everything down, reporting a step that fails rather than abandoning the rest."""
    # Set before anything stops, so the done callbacks can tell a task that failed from one that
    # was told to finish.
    running.shutdown.asked = True

    # Asked to stop rather than cancelled, so the delivery in hand finishes and the rest of its
    # batch goes back on the queue instead of sitting locked for the whole lease while the
    # replacement process polls an empty one.
    container.worker.stop()
    await safely(
        "stop the worker",
        stop(running.worker_task, grace=settings.worker_shutdown_grace_seconds),
    )
    if running.bot_task is not None:
        await safely("close the Discord client", bot.close())
        await safely("stop the bot", stop(running.bot_task))
    await safely("close the container", container.aclose())


def build_lifespan(bot: ShannonBot, container: Container, settings: Settings):
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await require_a_working_database(container.engine)
        except Exception as error:
            logger.error(
                "cannot reach the database, or it has never been migrated: %s. "
                "Check SHANNON_DATABASE_URL and run `alembic upgrade head`.",
                error,
            )
            raise

        liveness = ProcessLiveness(container.engine)
        app.state.liveness = liveness

        running = await _start(bot, container, settings, liveness)
        try:
            yield
        finally:
            await _close(bot, container, settings, running)

    return lifespan
