"""Holding one tracked item to one writer at a time, Discord calls included."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

# The first key of the per-item advisory lock, which makes a space of its own. Postgres keeps the
# two-integer keys apart from the single-bigint ones, and `UserLinkStore` uses the second with a
# guild id, so the two cannot collide however the numbers land.
_ONE_ITEM_AT_A_TIME = 8_531

# Which items this task is already holding. Only ever read to answer "is this one ours already",
# and rebound rather than mutated so a task cannot alter what another one sees.
_ours: ContextVar[frozenset[int]] = ContextVar("shannon_items_held", default=frozenset())


def _lock_key(github_object_id: int) -> int:
    """The item's own GitHub id, folded into the signed 32 bits an advisory key allows.

    GitHub numbers every issue and pull request uniquely, so this separates items rather than
    grouping them. Two items whose ids happen to fold together take turns for the length of a
    Discord call, which costs nothing and cannot be wrong.
    """
    return (github_object_id % 2**32) - 2**31


class ItemLock:
    """One writer at a time per item, across everything that writes to its thread.

    Its own class because two callers need it and neither owns it. The sync service takes it
    around a whole sync, and the status commands take it around the lock they set themselves,
    which is a Discord call the sync path never sees.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    @asynccontextmanager
    async def held(self, github_object_id: int) -> AsyncIterator[None]:
        """Hold this item until the block ends.

        The row's own lock already orders what happens in the database, and that was never the
        gap. Discord is called outside the transaction, deliberately, so that a slow gateway
        cannot hold a connection, which leaves two writers of one item free to interleave the
        calls themselves: a superseded snapshot was reproduced locking a thread a newer one had
        just unlocked. Two of them are ordinary rather than exotic, because `/pr` and
        `/set_done` run while an event for the same item is in flight.

        An advisory lock rather than a row lock, because this has to outlive the transaction
        that reads the row and cover the calls that come after it. Keyed on the item's GitHub
        id, which is known before anything is read, and taken before the read so the writer that
        waits goes on to read a row the other one has already written: the staleness guard then
        turns it away if it is the older of the two, which is the answer that was missing.

        Waited for rather than skipped. The one that waits may be the newer delivery carrying
        the work that matters, and a Discord phase is short.

        Tied to a transaction rather than handed back by name. A session-scoped lock has to be
        released explicitly, and the one moment that release cannot be made is the moment it
        matters most: a cancelled task raises at the next await it reaches, the release
        included, so a shutdown part way through would return the connection to the pool still
        holding the lock and that item would wait on it for ever. A transaction-scoped lock ends
        when its transaction does, however it ends, and the pool rolls a connection back before
        letting anything else have it.

        Taken again by a caller that already holds it costs nothing and blocks nothing. Without
        that, one caller doing two of these things to one item deadlocks against itself, which
        is not hypothetical: `/set_status` sets the lock on a thread and re-renders the item
        through the ordinary sync, and the sync takes this for itself. It happened once already,
        in a test that reproduced the very race this exists to close, and it stopped the whole
        integration tier with one connection holding the lock and one waiting on it.

        What this gives up is a connection held for the length of the Discord phase, one per
        item being written to at that moment, against a pool of fifteen and a handful ever in
        flight.
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
