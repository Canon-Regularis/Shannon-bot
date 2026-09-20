"""Startup and shutdown, in the order they have to happen."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
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
from shannon.services.verification import GitHubIdentityVerification

logger = logging.getLogger(__name__)

# Long enough for a cold connection and a first query on a loaded server, short enough that an
# orchestrator sees a process that failed to start rather than one that never answers.
STARTUP_CHECK_SECONDS = 15.0


class ProcessParts(Protocol):
    """What owning the process needs of the wiring, and nothing else.

    Container satisfies this by shape, so starting and stopping does not depend on the
    composition root.
    """

    # A mutable protocol member is invariant, so `Container.conversations: ConversationLog` would
    # be refused where the narrower `ReloadsConversations` is asked for; hence read-only.
    @property
    def engine(self) -> AsyncEngine: ...
    @property
    def worker(self) -> RunsDeliveries: ...
    @property
    def poller(self) -> PollsABoard: ...
    @property
    def conversations(self) -> ReloadsConversations: ...
    @property
    def flusher(self) -> FlushesTranscripts: ...
    @property
    def verification(self) -> GitHubIdentityVerification | None: ...

    async def aclose(self) -> None: ...


class RunsDeliveries(Protocol):
    """The delivery worker as the lifespan sees it."""

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None: ...

    def stop(self) -> None: ...


class PollsABoard(Protocol):
    """The project poller as the lifespan sees it.

    GitHub sends no project webhook for a personal account, so a board is polled by a second
    long-lived task rather than handled behind the queue.
    """

    @property
    def enabled(self) -> bool: ...

    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


class ReloadsConversations(Protocol):
    """Filling the set of threads being captured."""

    async def reload(self) -> None: ...


class FlushesTranscripts(Protocol):
    """The transcript flusher as the lifespan sees it.

    Unlike a board, publishing a conversation is on in every deployment, so there is no
    `enabled` flag to branch on.
    """

    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


class Gateway(Protocol):
    """The Discord connection as this process uses it.

    Named here rather than importing the client, so starting and stopping the process does not
    depend on discord.py.
    """

    async def start(self, token: str) -> None: ...

    async def wait_until_ready(self) -> None: ...

    def is_ready(self) -> bool: ...

    def gateway_is_up(self) -> bool:
        """Whether Discord can be reached right now, which is not what `is_ready` answers.

        `is_ready` reports whether the cache has ever been filled: it is set once and cleared
        only by `close`, so a connection that came up and later died still reads as ready.
        """
        ...

    async def close(self) -> None: ...


async def require_database(engine: AsyncEngine) -> None:
    """Prove the database answers and has been migrated before the port opens.

    Building an engine connects to nothing, so without this a wrong password or an unmigrated
    database still reaches "startup complete" and passes a health check while every delivery
    fails behind it. Deadlined because asyncpg's sixty seconds bound the handshake and not the
    query, and uvicorn opens no listening socket until startup returns: a server that accepts the
    connection and then goes quiet would leave the process unable to serve or to be stopped.
    """
    async with asyncio.timeout(STARTUP_CHECK_SECONDS), engine.connect() as connection:
        # Proves migrations have been applied at least once, not that they are at head: checking
        # head would mean loading the Alembic environment into the running app.
        await connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))


def gateway_ready(bot: Gateway, bot_task: asyncio.Task[None]) -> ReadyCheck:
    """Wait for the gateway, and give up if the bot stops trying to reach it.

    `wait_until_ready` waits on an event only ever set once a connection succeeds, so a bad token
    leaves it waiting for the rest of the process's life while the endpoint goes on accepting
    deliveries that no worker ever leases.
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
    shutdown: Shutdown
    worker_task: asyncio.Task[None]
    bot_task: asyncio.Task[None] | None
    poller_task: asyncio.Task[None] | None = None
    flusher_task: asyncio.Task[None] | None = None


async def _start(
    bot: Gateway,
    container: ProcessParts,
    settings: Settings,
    liveness: ProcessLiveness,
    halt: Callable[[], None],
) -> _Running:
    """Bring up the gateway and the worker, in that order.

    The worker waits for the gateway rather than racing it: acting on a delivery before Discord
    is connected only wastes an attempt.

    Only the bot and the worker pass `halt`. A process whose poller or flusher died still mirrors
    everything the webhooks bring it, and what the flusher has not published waits in the table
    until a process with a working one picks it up.
    """
    shutdown = Shutdown()
    bot_task: asyncio.Task[None] | None = None
    ready: ReadyCheck | None = None

    # Before the gateway, not after: a message cannot arrive before the connection is up, so this
    # is the last moment the set can be filled without a race. A conversation left out is one
    # armed in the database and captured by nothing.
    await container.conversations.reload()

    token = settings.discord_token.get_secret_value()
    if token:
        bot_task = asyncio.create_task(bot.start(token))
        # discord.py reconnects on its own, so this task ending at all means it gave up. The
        # worker waits for the gateway only once, at the start, so a connection lost after that
        # leaves it leasing deliveries that all fail.
        bot_task.add_done_callback(report_exit("Discord bot", shutdown, halt))
        ready = gateway_ready(bot, bot_task)
    else:
        logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

    worker_task = asyncio.create_task(container.worker.run_forever(ready))
    worker_task.add_done_callback(report_exit("delivery worker", shutdown, halt))

    flusher_task = asyncio.create_task(container.flusher.run_forever())
    flusher_task.add_done_callback(report_exit("transcript flusher", shutdown))

    # Starting a task that returns at once would have the done callback report the poller as
    # stopped, on every boot, for everybody not using a board.
    poller_task: asyncio.Task[None] | None = None
    if container.poller.enabled:
        poller_task = asyncio.create_task(container.poller.run_forever())
        poller_task.add_done_callback(report_exit("project poller", shutdown))

    # /health reads these: a dead worker leaves the endpoint answering 200 to deliveries nothing
    # will act on.
    liveness.worker_task = worker_task
    liveness.bot_task = bot_task
    liveness.poller_task = poller_task
    liveness.flusher_task = flusher_task
    # Safe at any point in a client's life: it reads a flag the client keeps from its own connect
    # and disconnect events, and `is_ready` behind it checks the sentinel before the event.
    liveness.gateway_is_ready = bot.gateway_is_up if bot_task is not None else None
    return _Running(
        shutdown=shutdown,
        worker_task=worker_task,
        bot_task=bot_task,
        poller_task=poller_task,
        flusher_task=flusher_task,
    )


async def _close(
    bot: Gateway, container: ProcessParts, settings: Settings, running: _Running
) -> None:
    # Set before anything stops, so the done callbacks can tell a task that failed from one that
    # was told to finish.
    running.shutdown.asked = True

    # Asked to stop rather than cancelled, so the delivery in hand finishes and the rest of the
    # batch goes back on the queue instead of sitting locked for the whole lease.
    container.worker.stop()
    container.poller.stop()
    container.flusher.stop()
    await safely(
        "stop the worker",
        stop(running.worker_task, grace=settings.worker_shutdown_grace_seconds),
    )
    await safely(
        "stop the project poller",
        stop(running.poller_task, grace=settings.worker_shutdown_grace_seconds),
    )
    # Asked rather than cancelled: a comment half sent may land with nothing recording that it
    # did, and the batch would then publish twice.
    await safely(
        "stop the transcript flusher",
        stop(running.flusher_task, grace=settings.worker_shutdown_grace_seconds),
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
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        try:
            await require_database(container.engine)
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
        # The OAuth callback reads this off app state: it is entered from outside the process
        # rather than called by anything inside it.
        app.state.verification = container.verification

        running = await _start(bot, container, settings, liveness, halt)
        try:
            yield
        finally:
            await _close(bot, container, settings, running)
            await safely("close the health probe engine", probes.dispose())

    return lifespan
