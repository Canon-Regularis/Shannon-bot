from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.db.models import WebhookEvent
from shannon.domain.enums import ObjectType
from shannon.github.client import GitHubClient
from shannon.services.delivery.worker import DeliveryWorker
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support.db import map_channel, register_repository
from tests.support.signing import SECRET, post


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


@asynccontextmanager
async def registered_stack(
    engine: AsyncEngine,
    session: AsyncSession,
    threads: FakeThreadGateway,
    *,
    issues_channel: int | None = 98,
) -> AsyncIterator[DeliveryClient]:
    """A registered repository with the whole stack over it, ready to take a delivery.

    Six files were building this by hand and differed only in whether issues had a channel and
    what had already been delivered. Those differences stay in the fixtures that care; the four
    lines they all repeated are here.

    `issues_channel=None` is the guild where nobody ran /set_channel for issues, which is the
    case the channel fallback exists for and must stay reachable.
    """
    repository = await register_repository(session, guild_id=1, channel_id=99)
    if issues_channel is not None:
        await map_channel(session, repository, ObjectType.ISSUE, channel_id=issues_channel)
    async with build_http_client(build_stack(engine, threads=threads)) as client:
        yield client


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


async def deliver(
    client: DeliveryClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str = "delivery-1",
) -> Response:
    """Post a webhook and let the worker act on it, which is the whole path in one call."""
    response = await post(client, event, payload, delivery=delivery)
    await client.drain()
    return response
