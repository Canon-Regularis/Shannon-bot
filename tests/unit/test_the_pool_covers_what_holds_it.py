"""The pool is at least as large as the work that holds it, and says so in one place.

SQLAlchemy's defaults are five connections and ten overflow. The background work alone was
already written to hold fifteen - eight transcriptions, two thread deletions, two each for the
delivery worker and the project poller, one for the transcript flusher - and each of those
limits was chosen in its own file with no view of the others or of the total.

So the endpoint drew from nothing. A saturated pool had `enqueue` wait out SQLAlchemy's thirty
second default and raise, three times the ten seconds GitHub allows, and GitHub does not
redeliver a delivery it recorded as failed. The arithmetic is the whole defect, so this holds
it: the stated ceiling against the limits it counts, and the wait against GitHub's budget.
"""

from __future__ import annotations

from sqlalchemy.pool import QueuePool

from shannon.db.session import (
    BACKGROUND_CONNECTIONS,
    POOL_RECYCLE_SECONDS,
    POOL_TIMEOUT_SECONDS,
    build_engine,
)
from shannon.discord_bot.client import LETTING_GO_AT_ONCE, TRANSCRIBING_AT_ONCE

URL = "postgresql+asyncpg://user:password@localhost:5432/shannon"

# What the webhook route has to answer within, from GitHub's own documentation.
GITHUB_ALLOWS_SECONDS = 10.0

# The delivery worker and the project poller each hold the item lock and open a second session
# inside it; the transcript flusher takes no lock.
LOCKED_WORK = 2 + 2
FLUSHER = 1


def the_pool() -> QueuePool:
    """The engine's pool, narrowed: only a queueing one has a size to read off it."""
    pool = build_engine(URL).pool
    assert isinstance(pool, QueuePool)
    return pool


def test_the_stated_ceiling_counts_every_limit_there_is() -> None:
    """`db` sits below `discord_bot` and cannot import the semaphores, so this is the seam.

    Raising a semaphore without raising `BACKGROUND_CONNECTIONS` is the change that puts the
    endpoint back to drawing from nothing, and it fails here rather than in production.
    """
    assert BACKGROUND_CONNECTIONS == (
        LETTING_GO_AT_ONCE + TRANSCRIBING_AT_ONCE + LOCKED_WORK + FLUSHER
    )


def test_the_pool_is_larger_than_the_background_work() -> None:
    """Room left over is the point: everything counted above runs whether or not anyone is
    waiting, and what waits is a slash command or a delivery."""
    assert the_pool().size() > BACKGROUND_CONNECTIONS


def test_a_burst_is_allowed_beyond_the_pool() -> None:
    assert the_pool()._max_overflow > 0


def test_waiting_for_a_connection_ends_inside_githubs_budget() -> None:
    """The 503 the route answers on this is worth nothing if it arrives after GitHub gave up."""
    assert POOL_TIMEOUT_SECONDS < GITHUB_ALLOWS_SECONDS
    assert the_pool()._timeout == POOL_TIMEOUT_SECONDS


def test_a_connection_is_not_kept_forever() -> None:
    """SQLAlchemy's default is -1. A socket dropped by a NAT in between is reported to neither
    end, and the pre-ping that would find that out is itself what hangs on a dead one."""
    assert POOL_RECYCLE_SECONDS > 0
    assert the_pool()._recycle == POOL_RECYCLE_SECONDS
