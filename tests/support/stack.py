from __future__ import annotations

import json
from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.github.client import GitHubClient
from shannon.github.webhooks.signature import sign
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway

SECRET = "test-webhook-secret"


def build_stack(
    engine: AsyncEngine,
    *,
    threads: FakeThreadGateway | None = None,
    github: GitHubClient | None = None,
) -> Container:
    """The real container with Discord and GitHub swapped for fakes.

    Everything else is production code: the same router, the same sync service, the same
    signature check, the same database.
    """
    return build_container(
        threads=threads or FakeThreadGateway(),
        settings=Settings(github_webhook_secret=SECRET),
        engine=engine,
        github=github or FakeGitHubClient(),
    )


def build_http_client(container: Container) -> AsyncClient:
    app = create_app(
        settings=container.settings,
        event_router=container.event_router,
        delivery_guard=container.delivery_guard,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def deliver(
    client: AsyncClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
) -> Response:
    """Post a webhook the way GitHub does, signature and all."""
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(body, SECRET),
        },
    )
