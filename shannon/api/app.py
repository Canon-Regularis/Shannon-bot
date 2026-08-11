from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from shannon.api.routes import webhooks
from shannon.config import Settings, get_settings
from shannon.github.webhooks.events import EventRouter
from shannon.services.idempotency import DeliveryGuard


def create_app(
    *,
    settings: Settings | None = None,
    event_router: EventRouter | None = None,
    delivery_guard: DeliveryGuard | None = None,
    lifespan: Callable[[FastAPI], Any] | None = None,
) -> FastAPI:
    """Build the ASGI app.

    Collaborators are arguments rather than module globals so tests can hand in their own
    router without touching the environment. Leaving delivery_guard out skips duplicate
    detection, which is what the route-level tests want.
    """
    app = FastAPI(title="Shannon Bot", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.state.event_router = event_router or EventRouter()
    app.state.delivery_guard = delivery_guard
    app.include_router(webhooks.router)
    return app
