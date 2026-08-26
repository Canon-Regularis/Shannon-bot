from __future__ import annotations

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build the engine, saying which setting is wrong when the URL will not parse.

    SQLAlchemy's own message is "Could not parse SQLAlchemy URL from string ''", which is
    accurate and names nothing an operator can go and change. This runs before uvicorn has
    started, so it is the first thing anybody sees, and blank is the shape it usually takes:
    `SHANNON_DATABASE_URL=` left empty in a copied `.env` reads as set and is not.
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

    Pooled connections are the problem rather than the point here. The engine everything else
    uses pre-pings on checkout, which is right for work that must not be handed a connection
    that died while it sat in the pool. It is wrong for a question with a deadline on it: when
    the deadline cancels a pre-ping, SQLAlchemy treats the connection as failed and terminates
    it, and terminating an asyncpg connection opens a second socket to send the cancel and waits
    on that one with nothing bounding it. Measured against a frozen database: the first health
    check after the outage began answered nothing for eleven minutes, and every later one queued
    behind it, so the endpoint that exists to report an outage was the one thing the outage
    silenced.

    No pool and no pre-ping, so every probe opens its own connection and the deadline is the
    only thing deciding how long it waits. It costs one connection per probe, at most one per
    `probe_every`, against a question nobody asks more often than that.
    """
    return create_async_engine(engine.url, poolclass=NullPool, pool_pre_ping=False)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
