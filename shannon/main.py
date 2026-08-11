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
        delivery_guard=container.delivery_guard,
        lifespan=_lifespan(bot, container, settings),
    )


def _lifespan(bot: ShannonBot, container: Container, settings: Settings):
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task | None = None
        token = settings.discord_token.get_secret_value()
        if token:
            task = asyncio.create_task(bot.start(token))
        else:
            # Handy for poking the webhook endpoint locally, and loud enough that nobody
            # deploys like this by accident.
            logger.warning("SHANNON_DISCORD_TOKEN is not set, running without the bot")

        try:
            yield
        finally:
            if task is not None:
                await bot.close()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await container.aclose()

    return lifespan


def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
