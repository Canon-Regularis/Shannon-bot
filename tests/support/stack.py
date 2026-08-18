from __future__ import annotations

import json
from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.db.models import WebhookEvent
from shannon.github.client import GitHubClient
from shannon.github.webhooks.signature import sign
from shannon.services.delivery.worker import DeliveryWorker
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


class DeliveryClient:
    """The endpoint and the worker behind it, driven as one.

    The endpoint only writes a delivery down, so a test that posts one and then looks at Discord
    has to run the worker in between. Doing that here keeps it out of every test.
    """

    def __init__(
        self, app_client: AsyncClient, worker: DeliveryWorker, sessionmaker: async_sessionmaker
    ) -> None:
        self.http = app_client
        self.worker = worker
        self._sessionmaker = sessionmaker

    async def __aenter__(self) -> DeliveryClient:
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.__aexit__(*exc)

    async def post(self, *args: Any, **kwargs: Any) -> Response:
        return await self.http.post(*args, **kwargs)

    async def drain(self) -> None:
        """Work through the queue until it is empty."""
        while await self.worker.run_once():
            pass

    async def outcome_of(self, delivery: str) -> str:
        """What the worker made of a delivery.

        The response no longer carries this. The endpoint answers before anything has been
        tried, so whether there was work to do is only known once the worker has run.
        """
        async with self._sessionmaker() as session:
            status = await session.scalar(
                select(WebhookEvent.status).where(WebhookEvent.github_delivery_id == delivery)
            )
        return str(status).lower() if status is not None else "not queued"


def build_http_client(container: Container) -> DeliveryClient:
    app = create_app(
        settings=container.settings,
        event_router=container.event_router,
        queue=container.queue,
    )
    return DeliveryClient(
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        container.worker,
        container.sessionmaker,
    )


async def send(
    client: DeliveryClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
) -> Response:
    """Post a webhook the way GitHub does, signature and all, and stop there."""
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


async def deliver(
    client: DeliveryClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
) -> Response:
    """Post a webhook and let the worker act on it, which is the whole path in one call."""
    response = await send(client, event, payload, delivery=delivery)
    await client.drain()
    return response
