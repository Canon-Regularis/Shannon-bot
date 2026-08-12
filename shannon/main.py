from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from shannon.api.app import create_app
from shannon.config import Settings, get_settings
from shannon.container import Container, build_container
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.threads import DiscordThreadGateway

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


async def _stop(task: asyncio.Task | None, *, grace: float = 0.0) -> None:
    """Wait `grace` seconds for a task to finish on its own, then cancel it."""
    if task is None:
        return

    if grace > 0:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        if task.done():
            return
        logger.warning("a background task did not stop within %ss, cancelling it", grace)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _lifespan(bot: ShannonBot, container: Container, settings: Settings):
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bot_task: asyncio.Task | None = None
        token = settings.discord_token.get_secret_value()
        if token:
            bot_task = asyncio.create_task(bot.start(token))
            bot_task.add_done_callback(_report_exit("Discord bot"))
        else:
            # Handy for poking the webhook endpoint locally, and loud enough that nobody
            # deploys like this by accident.
            logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

        # Deliveries are only written down by the endpoint. Without this running, they queue up
        # and nothing reaches Discord.
        worker_task = asyncio.create_task(container.worker.run_forever())
        worker_task.add_done_callback(_report_exit("delivery worker"))

        try:
            yield
        finally:
            # Asked to stop rather than cancelled, so the delivery in hand finishes and the
            # rest of its batch goes back on the queue instead of sitting locked for the whole
            # lease while the replacement process polls an empty one.
            container.worker.stop()
            await _stop(worker_task, grace=settings.worker_shutdown_grace_seconds)
            if bot_task is not None:
                await bot.close()
                await _stop(bot_task)
            await container.aclose()

    return lifespan


def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
