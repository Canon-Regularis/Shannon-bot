from __future__ import annotations

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# What the background work holds when all of it is busy: `LETTING_GO_AT_ONCE` thread deletions
# and `TRANSCRIBING_AT_ONCE` transcriptions in `discord_bot.client`, then the delivery worker and
# the project poller at two apiece - `ItemLock.held` keeps a connection for the length of the
# block and the sync inside it opens its own - and the transcript flusher, which takes no lock,
# at one. Stated here as a literal because `db` is below `discord_bot` and may not read it;
# `test_the_pool_covers_what_holds_it` is what holds the two in step.
#
# Anyone raising a limit there has to raise this with it. Left unchanged, what runs out is the
# webhook endpoint, which is the one caller GitHub will not wait for.
BACKGROUND_CONNECTIONS = 2 + 8 + 2 + 2 + 1

# Slash commands and webhook deliveries on top of that, neither bounded by anything here. A
# command that syncs takes two as well.
SPARE_CONNECTIONS = 10
BURST_CONNECTIONS = 10

# Under the ten seconds GitHub allows the endpoint, so a saturated pool answers 503 inside the
# budget and the delivery is redelivered. SQLAlchemy's own default is thirty.
POOL_TIMEOUT_SECONDS = 5.0

# A connection is not kept forever: an idle socket dropped by a NAT in between is reported to
# neither end, and the pre-ping that would discover it is itself what hangs on a dead one.
POOL_RECYCLE_SECONDS = 1800


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build the engine, saying which setting is wrong when the URL will not parse.

    SQLAlchemy's own message, "Could not parse SQLAlchemy URL from string ''", names nothing an
    operator can change; blank is usual, from `SHANNON_DATABASE_URL=` empty in a copied `.env`.
    """
    try:
        return create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=BACKGROUND_CONNECTIONS + SPARE_CONNECTIONS,
            max_overflow=BURST_CONNECTIONS,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            pool_recycle=POOL_RECYCLE_SECONDS,
        )
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
