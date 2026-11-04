from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI

from shannon.api.dependencies import EventIntake
from shannon.api.routes import health, oauth, webhooks
from shannon.config import Settings, get_settings
from shannon.github.webhooks.router import EventRouter
from shannon.services.delivery.queue import DeliveryInbox


def create_app(
    *,
    settings: Settings | None = None,
    event_router: EventIntake | None = None,
    queue: DeliveryInbox | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Build the ASGI app. Leaving the queue out makes the route do its work inline."""
    # FastAPI traces every request by itself once any OpenTelemetry provider is set up in the
    # process, and its span keeps the query string word for word: on the OAuth callbacks that is
    # the `code` and the `state` that `oauth.py` promises are never logged. Nothing here sets a
    # provider up; this keeps the promise on the day something does.
    app = FastAPI(
        title="Shannon Bot",
        version="0.1.0",
        lifespan=lifespan,
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.state.settings = settings or get_settings()
    app.state.event_router = event_router or EventRouter()
    app.state.delivery_queue = queue
    # Set by the lifespan once the worker exists. Without it /health can only report that the
    # port is open.
    app.state.liveness = None
    # Set by the lifespan once the container exists. The OAuth routes are entered from outside
    # rather than called, so they read what they need off app state.
    app.state.verification = None
    app.include_router(webhooks.router)
    app.include_router(health.router)
    app.include_router(oauth.router)
    return app
