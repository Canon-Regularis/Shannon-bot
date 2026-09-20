"""Holding one tracked item to one writer at a time, Discord calls included."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# The first key of the per-item advisory lock. Postgres keeps two-integer keys apart from
# single-bigint ones, and `UserLinkStore` uses the latter, so the two spaces cannot collide.
_ONE_ITEM_AT_A_TIME = 8_531

# Which items this task holds, rebound rather than mutated so a task cannot alter another's.
_ours: ContextVar[frozenset[int]] = ContextVar("shannon_items_held", default=frozenset())


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

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    @asynccontextmanager
    async def held(self, github_object_id: int) -> AsyncGenerator[None]:
        """Hold this item until the block ends.

        Discord is called outside the transaction so a slow gateway cannot hold a connection,
        which lets two writers of one item interleave their calls: a superseded snapshot was
        reproduced locking a thread a newer one had just unlocked, and `/pr` runs while an
        event for the same item is in flight.

        Advisory, so it outlives the transaction that reads the row, and taken before that read
        so the waiting writer reads what the other wrote and the staleness guard can turn it
        away. Transaction-scoped because a cancelled task raises at the explicit release a
        session-scoped lock needs, leaving the pooled connection holding it. Re-entrant because
        `/set_status` re-renders through the sync, which takes this lock again.
        """
        key = _lock_key(github_object_id)
        if key in _ours.get():
            yield
            return

        token = _ours.set(_ours.get() | {key})
        try:
            async with self._sessionmaker() as session:
                await session.execute(select(func.pg_advisory_xact_lock(_ONE_ITEM_AT_A_TIME, key)))
                yield
        finally:
            _ours.reset(token)
