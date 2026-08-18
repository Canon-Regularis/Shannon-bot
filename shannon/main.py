from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field

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


@dataclass(slots=True)
class _Shutdown:
    """Whether the process asked for what is about to happen.

    A background task ending is an emergency or a formality, and nothing about the task says
    which.
    """

    asked: bool = False


def _report_exit(what: str, shutdown: _Shutdown):
    """Say why a background task stopped, when nobody asked it to.

    A task that dies otherwise takes its exception with it while the endpoint carries on
    answering.

    Quiet once a stop has been asked for, since a clean shutdown ends these tasks exactly as a
    failure does: the worker's loop returns when told, and the Discord client's start returns
    when closed. Warning either way meant the one line that says the process is now useless
    appeared on every restart. Errors are still reported whichever way it is going.
    """

    def report(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("the %s stopped: %s", what, error, exc_info=error)
        elif not shutdown.asked:
            logger.warning("the %s stopped without an error", what)

    return report


async def _safely(what: str, closing: Awaitable[None]) -> None:
    """Run one shutdown step, reporting a failure rather than raising it.

    One step failing must not take the rest with it: a database pool, an HTTP client and a
    gateway connection all need closing.
    """
    try:
        await closing
    except Exception as error:
        logger.error("could not %s while shutting down: %s", what, error)


async def _stop(task: asyncio.Task | None, *, grace: float = 0.0) -> None:
    """Wait `grace` seconds for a task to finish on its own, then cancel it.

    Watches the task rather than awaiting its result. Awaiting adopts the exception of a task
    that has already died, which would raise out of the first step of shutdown and leave
    everything after it unclosed. The done callback has already reported it.
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
        shutdown = _Shutdown()

        bot_task: asyncio.Task | None = None
        ready: ReadyCheck | None = None
        token = settings.discord_token.get_secret_value()
        if token:
            bot_task = asyncio.create_task(bot.start(token))
            bot_task.add_done_callback(_report_exit("Discord bot", shutdown))
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
        worker_task.add_done_callback(_report_exit("delivery worker", shutdown))
        # A worker that dies takes the whole point of the process with it, and the endpoint
        # would go on answering 200 to deliveries nothing will act on. /health is what makes
        # that visible from outside.
        liveness.worker_task = worker_task
        liveness.bot_task = bot_task

        try:
            yield
        finally:
            # Set before anything is stopped, so the done callbacks can tell a task that failed
            # from one that was told to finish.
            shutdown.asked = True
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
