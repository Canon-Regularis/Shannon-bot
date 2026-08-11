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

TABLES = (
    "item_assignments",
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
