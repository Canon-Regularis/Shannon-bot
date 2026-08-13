from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from shannon.api.app import create_app
from shannon.config import Settings, get_settings
from shannon.container import Container, build_container
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.threads import DiscordThreadGateway
from shannon.services.worker import ReadyCheck

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the whole application behind one ASGI app.

    The bot and the webhook endpoint share a process, so a webhook can reach Discord directly
    with no queue in between.
    """
    settings = settings or get_settings()

    bot = ShannonBot()
    container = build_container(threads=DiscordThreadGateway(bot), settings=settings)
    bot.install(*container.commands())

    return create_app(
        settings=settings,
        event_router=container.event_router,
        queue=container.queue,
        lifespan=_lifespan(bot, container, settings),
    )


def _report_exit(what: str):
    """Say why a background task stopped.

    Without this a task that dies takes its exception with it, and the endpoint carries on
    answering while nothing behind it works.
    """

    def report(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("the %s stopped: %s", what, error, exc_info=error)
        else:
            logger.warning("the %s stopped without an error", what)

    return report


async def _safely(what: str, closing: Awaitable[None]) -> None:
    """Run one shutdown step, reporting a failure rather than raising it.

    One step failing must not take the rest with it. What goes unclosed is a database pool, an
    HTTP client and a gateway connection, and a process that cannot shut down cleanly is one an
    orchestrator ends up killing instead.
    """
    try:
        await closing
    except Exception as error:
        logger.error("could not %s while shutting down: %s", what, error)


async def _stop(task: asyncio.Task | None, *, grace: float = 0.0) -> None:
    """Wait `grace` seconds for a task to finish on its own, then cancel it.

    This watches the task rather than awaiting its result. A task that has already died takes
    its exception with it, and adopting that here would raise out of the shutdown path: this is
    the first thing the lifespan does when closing, so everything after it, the Discord client
    and the engine and the HTTP client, would never be closed at all. The exception has already
    been reported by the done callback.
    """
    if task is None:
        return

    if grace > 0:
        await asyncio.wait({task}, timeout=grace)
        if task.done():
            return
        logger.warning("a background task did not stop within %ss, cancelling it", grace)

    task.cancel()
    await asyncio.wait({task})


@dataclass(slots=True)
class ProcessLiveness:
    """What /health reports, kept where the things it reports on actually live."""

    engine: AsyncEngine
    worker_task: asyncio.Task | None = None
    # None when no token was configured, which is the deliberate no-bot mode rather than a
    # failure. Anything else and a finished task means the gateway has gone.
    bot_task: asyncio.Task | None = None
    # A probe is reused for this long. The route is open to anyone who can reach the port, and
    # a connection per request would let a flood exhaust the pool the worker runs on, which is
    # the very thing this endpoint exists to notice.
    probe_every: float = 5.0
    _probed_at: float = 0.0
    _reachable: bool = False

    async def database_reachable(self) -> bool:
        now = time.monotonic()
        if self._probed_at and now - self._probed_at < self.probe_every:
            return self._reachable

        self._probed_at = now
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:
            logger.warning("the database is not reachable: %s", error)
            self._reachable = False
        else:
            self._reachable = True
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


async def _require_a_working_database(engine: AsyncEngine) -> None:
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


def _connected(bot: ShannonBot, bot_task: asyncio.Task) -> ReadyCheck:
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


def _lifespan(bot: ShannonBot, container: Container, settings: Settings):
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await _require_a_working_database(container.engine)
        except Exception as error:
            logger.error(
                "cannot reach the database, or it has never been migrated: %s. "
                "Check SHANNON_DATABASE_URL and run `alembic upgrade head`.",
                error,
            )
            raise

        liveness = ProcessLiveness(container.engine)
        app.state.liveness = liveness

        bot_task: asyncio.Task | None = None
        ready: ReadyCheck | None = None
        token = settings.discord_token.get_secret_value()
        if token:
            bot_task = asyncio.create_task(bot.start(token))
            bot_task.add_done_callback(_report_exit("Discord bot"))
            ready = _connected(bot, bot_task)
        else:
            # Handy for poking the webhook endpoint locally, and loud enough that nobody
            # deploys like this by accident.
            logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

        # Deliveries are only written down by the endpoint. Without this running, they queue up
        # and nothing reaches Discord. It waits for the bot rather than racing it: the endpoint
        # accepts deliveries from the moment the port is open, which is the point, but acting on
        # one before Discord is connected only wastes an attempt.
        worker_task = asyncio.create_task(container.worker.run_forever(ready))
        worker_task.add_done_callback(_report_exit("delivery worker"))
        # A worker that dies takes the whole point of the process with it, and the endpoint
        # would go on answering 200 to deliveries nothing will act on. /health is what makes
        # that visible from outside.
        liveness.worker_task = worker_task
        liveness.bot_task = bot_task

        try:
            yield
        finally:
            # Asked to stop rather than cancelled, so the delivery in hand finishes and the
            # rest of its batch goes back on the queue instead of sitting locked for the whole
            # lease while the replacement process polls an empty one.
            container.worker.stop()
            await _safely(
                "stop the worker",
                _stop(worker_task, grace=settings.worker_shutdown_grace_seconds),
            )
            if bot_task is not None:
                await _safely("close the Discord client", bot.close())
                await _safely("stop the bot", _stop(bot_task))
            await _safely("close the container", container.aclose())

    return lifespan


def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
