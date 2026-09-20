from __future__ import annotations

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build the engine, saying which setting is wrong when the URL will not parse.

    SQLAlchemy's own message, "Could not parse SQLAlchemy URL from string ''", names nothing an
    operator can change; blank is usual, from `SHANNON_DATABASE_URL=` empty in a copied `.env`.
    """
    try:
        return create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    except ArgumentError as error:
        raise ArgumentError(
            f"SHANNON_DATABASE_URL is not a database URL: {error}. It should look like "
            "postgresql+asyncpg://user:password@host:5432/database"
        ) from error


def build_probe_engine(engine: AsyncEngine) -> AsyncEngine:
    """A second engine, for asking whether the database answers.

    A cancelled pre-ping has SQLAlchemy terminate the asyncpg connection, which opens a second
    socket for the cancel and waits on it unbounded: one health check stalled eleven minutes.
    """
    return create_async_engine(engine.url, poolclass=NullPool, pool_pre_ping=False)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
