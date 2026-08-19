"""Fixtures for the tier that needs a real PostgreSQL.

The database fixtures live here rather than in the root conftest because nothing under
tests/unit uses them. Keeping them out of reach is what makes a unit test that secretly needs a
database fail at collection instead of quietly skipping.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from shannon.db.base import Base
from shannon.db.models import Repository
from shannon.discord_bot.formatting import format_assignee_ping, format_reviewer_ping
from shannon.domain.enums import ActorRole, ObjectType
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.delivery.queue import WebhookDeliveryQueue
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import map_channel, register_repository

TABLES = (
    "item_assignments",
    "mirrored_notes",
    "tracked_items",
    "channel_mappings",
    "user_links",
    "webhook_events",
    "repositories",
)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SHANNON_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("SHANNON_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def prepared_database(database_url: str) -> str:
    """Rebuild the schema once per test session, straight from the models."""

    async def rebuild() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(rebuild())
    return database_url


@pytest_asyncio.fixture
async def db_engine(prepared_database: str) -> AsyncIterator[AsyncEngine]:
    """An engine on an empty database.

    Built and disposed inside each test's own event loop, because pooled asyncpg connections
    cannot cross loops. Pooling within a test is what keeps the integration tier from paying a
    fresh connection handshake for every session the services open.
    """
    engine = create_async_engine(prepared_database, pool_size=10, max_overflow=10)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def db_sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def queue(db_sessionmaker: async_sessionmaker) -> WebhookDeliveryQueue:
    return WebhookDeliveryQueue(db_sessionmaker)


@pytest.fixture
async def registered(db_session: AsyncSession) -> Repository:
    """A repository with both channel mappings, as /register then /set_channel leave things."""
    repository = await register_repository(db_session)
    await map_channel(db_session, repository, ObjectType.ISSUE, channel_id=98)
    return repository


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest.fixture
def sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    """Pull request sync without the notifier, for tests that are not about pinging."""
    return build_item_sync(db_sessionmaker, threads, PullRequestPolicy())


@pytest.fixture
def notifying_sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return build_item_sync(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
        ),
    )


@pytest.fixture
def issue_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return build_item_sync(db_sessionmaker, threads, IssuePolicy())


@pytest.fixture
def notifying_issue_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return build_item_sync(
        db_sessionmaker,
        threads,
        IssuePolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.ASSIGNEE, render=format_assignee_ping
        ),
    )


@pytest.fixture
def pr_event():
    def build(action: str = "opened", **overrides: object):
        snapshot = parse_pull_request_event(
            action, payloads.pull_request_event(action, **overrides)
        )
        assert snapshot is not None, f"payload for {action} did not parse"
        return snapshot

    return build


@pytest.fixture
def issue_event():
    def build(action: str = "opened", **overrides: object):
        snapshot = parse_issue_event(action, payloads.issue_event(action, **overrides))
        assert snapshot is not None, f"payload for {action} did not parse"
        return snapshot

    return build
