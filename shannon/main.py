from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from shannon.api.access_log import keep_oauth_queries_out_of_the_access_log
from shannon.api.app import create_app
from shannon.commands._replies import reply_for
from shannon.config import Settings, get_settings
from shannon.container import build_container
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.threads import DiscordThreadGateway
from shannon.runtime.lifespan import build_lifespan

# How long a stop waits for the requests still open, before uvicorn cancels them and starts the
# shutdown (#272). Uvicorn's own default is no limit at all, so one client that sent half a body
# and went quiet held the whole shutdown open: the loops were never asked to stop, and a process
# that halted itself never ended, so nothing restarted it.
#
# Ten, because GitHub waits ten seconds for an answer: past that there is nobody left to answer.
# Above the pool's wait (`POOL_TIMEOUT_SECONDS`), so a delivery waiting out a saturated pool still
# gets its 503. It runs before every wait of the shutdown, so it counts toward `stop_grace_period`.
HTTP_GRACE_SECONDS = 10


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the whole application behind one ASGI app.

    The bot and the webhook endpoint share a process; deliveries still go through the queue.
    """
    settings = settings or get_settings()

    # The one privileged intent this bot asks for, and only when a deployment has said so; see
    # `build_intents` for what it costs.
    bot = ShannonBot(explain_error=reply_for, capture_messages=settings.capture_discord_messages)
    container = build_container(threads=DiscordThreadGateway(bot), settings=settings)
    bot.install(*container.commands)
    # A second step because the gateway has to exist before the container that needs it.
    bot.tell_when_a_thread_goes(container.forget_thread)
    bot.tell_when_a_channel_goes(container.forget_channel)
    bot.tell_when_a_message_arrives(container.conversations)

    return create_app(
        settings=settings,
        event_router=container.event_router,
        queue=container.queue,
        lifespan=build_lifespan(bot, container, settings),
    )


def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    keep_oauth_queries_out_of_the_access_log()
    uvicorn.run(
        build_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        timeout_graceful_shutdown=HTTP_GRACE_SECONDS,
    )


if __name__ == "__main__":
    run()
