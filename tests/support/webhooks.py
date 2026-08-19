from __future__ import annotations

import json
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.github.webhooks.router import EventRouter
from shannon.github.webhooks.signature import sign
from tests.fakes.handlers import RecordingHandler

SECRET = "test-webhook-secret"


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


async def post(
    client: AsyncClient,
    event: str,
    payload: Any = None,
    *,
    body: bytes | None = None,
    delivery: str = "delivery-1",
    signature: str | object | None = ...,
    secret: str = SECRET,
) -> Response:
    """Post a webhook, signing the body the way GitHub does unless told otherwise."""
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if event:
        headers["X-GitHub-Event"] = event
    if delivery:
        headers["X-GitHub-Delivery"] = delivery

    header_signature = sign(raw, secret) if signature is ... else signature
    if header_signature is not None:
        headers["X-Hub-Signature-256"] = str(header_signature)

    return await client.post("/webhooks/github", content=raw, headers=headers)
