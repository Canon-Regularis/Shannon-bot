from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from shannon.api.app import create_app
from shannon.commands._replies import reply_for
from shannon.config import Settings, get_settings
from shannon.container import build_container
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.threads import DiscordThreadGateway
from shannon.runtime.lifespan import build_lifespan

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the whole application behind one ASGI app.

    The bot and the webhook endpoint share a process. Deliveries still go through the queue;
    sharing a process only saves them a network hop to reach Discord.
    """
    settings = settings or get_settings()

    bot = ShannonBot(explain_error=reply_for)
    container = build_container(threads=DiscordThreadGateway(bot), settings=settings)
    bot.install(*container.commands)
    # Both of these are a second step for the same reason: the gateway has to exist before the
    # container that needs it, so neither can be handed to the constructor.
    bot.tell_when_a_thread_goes(container.forget_thread)
    bot.tell_when_a_channel_goes(container.forget_channel)

    return create_app(
        settings=settings,
        event_router=container.event_router,
        queue=container.queue,
        lifespan=build_lifespan(bot, container, settings),
    )


def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
