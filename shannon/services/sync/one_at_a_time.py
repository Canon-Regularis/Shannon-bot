"""Holding one tracked item to one writer at a time, Discord calls included."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.domain.errors import ShannonError

logger = logging.getLogger(__name__)

# The first key of the per-item advisory lock. Postgres keeps two-integer keys apart from
# single-bigint ones, and `UserLinkStore` uses the latter, so the two spaces cannot collide.
_ONE_ITEM_AT_A_TIME = 8_531

# Which items this task holds, rebound rather than mutated so a task cannot alter another's.
# Keyed on the GitHub id rather than the folded key below, so two items that fold together
# cannot have one read as held because the other is.
_ours: ContextVar[frozenset[int]] = ContextVar("shannon_items_held", default=frozenset())

# How long a writer waits for an item somebody else is holding.
#
# Bounded rather than open-ended, and bounded here rather than by the caller: the worker cancels
# a handler at `worker_delivery_timeout_seconds`, and cancelling a task parked in
# `pg_advisory_xact_lock` has asyncpg open a second socket for the cancel and wait on that with
# nothing bounding it - the same failure `db.session` describes for the health probe. Postgres
# ending the wait itself means nothing has to cancel anything.
#
# Well under that budget, so a writer that loses the wait is still inside its deadline and there
# is time left for the sync it came to do. `test_the_item_wait_fits_the_delivery_budget` holds
# the two numbers in step.
LOCK_WAIT_SECONDS = 15.0

# Postgres' own code for a wait the session asked to be cut short.
_LOCK_NOT_AVAILABLE = "55P03"


class ItemBusyError(ShannonError):
    """Another writer held this item for longer than the wait allows.

    Not permanent: the other writer is almost always about to finish, so the worker's backoff
    and a second attempt by hand both get through.
    """


def _lock_key(github_object_id: int) -> int:
    """The item's own GitHub id, folded into the signed 32 bits an advisory key allows.

    GitHub ids are unique across issues and pull requests, so this separates items; two that
    fold together merely take turns for the length of a Discord call.
    """
    return (github_object_id % 2**32) - 2**31


class ItemLock:
    """One writer at a time per item, across everything that writes to its thread.

    Taken by the sync service around a whole sync, and by the status commands around their call.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        wait_for: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._wait_for = wait_for

    @asynccontextmanager
    async def held(self, github_object_id: int) -> AsyncGenerator[None]:
        """Hold this item until the block ends, or raise `ItemBusyError` having waited.

        What it is for: the row's own lock settles in the database and is let go at the commit,
        which is before the first Discord call, so two writers of one item were free to
        interleave everything after that. A superseded snapshot was reproduced locking a thread
        a newer one had just unlocked, and `/pr` runs while an event for the same item is in
        flight.

        The cost is that a connection is held for the length of the block, Discord calls
        included, and there is no way around that: the lock is transaction-scoped because a
        cancelled task raises at the explicit release a session-scoped one needs, leaving the
        pooled connection holding it. `db.session` counts this hold in its ceiling.

        Taken before the row is read, so the waiting writer reads what the other wrote and the
        staleness guard can turn it away. Re-entrant because `/set_status` re-renders through
        the sync, which takes this lock again.
        """
        if github_object_id in _ours.get():
            yield
            return

        token = _ours.set(_ours.get() | {github_object_id})
        try:
            async with self._sessionmaker() as session:
                await self._take(session, github_object_id)
                yield
        finally:
            _ours.reset(token)

    async def _take(self, session: AsyncSession, github_object_id: int) -> None:
        """Wait for the item, for as long as this lock allows and no longer.

        `set_config(..., true)` rather than `SET LOCAL`, which takes no parameter. Both are
        scoped to the transaction the first statement here opens, so the deadline goes back with
        the connection and no later borrower of it inherits one.
        """
        await session.execute(
            select(func.set_config("lock_timeout", f"{int(self._wait_for * 1000)}ms", True))
        )
        try:
            await session.execute(
                select(func.pg_advisory_xact_lock(_ONE_ITEM_AT_A_TIME, _lock_key(github_object_id)))
            )
        except DBAPIError as error:
            # Read off the driver's own exception: SQLAlchemy wraps asyncpg's
            # `LockNotAvailableError` in a plain `DBAPIError` whose class says nothing.
            if getattr(error.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            logger.info(
                "gave up waiting %ss for item %s, which something else is still writing to",
                self._wait_for,
                github_object_id,
            )
            raise ItemBusyError(
                f"Item {github_object_id} is being changed by something else."
            ) from error
