from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.github.webhooks.router import EventRouter
from tests.fakes.handlers import RecordingHandler
from tests.support.signing import SECRET, post

__all__ = ["SECRET", "build_client", "post"]


def build_client(
    handler: RecordingHandler | None,
    *,
    secret: str = SECRET,
    settings: Settings | None = None,
    queue: Any = None,
) -> AsyncClient:
    """A client for route-level tests.

    With no queue the route runs the handler inline, which keeps these tests about HTTP.
    """
    event_router = EventRouter()
    if handler is not None:
        event_router.register("pull_request", handler)
    app = create_app(
        settings=settings or Settings(github_webhook_secret=secret),
        event_router=event_router,
        queue=queue,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
