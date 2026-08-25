from __future__ import annotations

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


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


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
