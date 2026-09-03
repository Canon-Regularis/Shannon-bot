"""One item held to one writer at a time, which is the only thing ordering the Discord calls.

The row's own lock settles what happens in the database and is let go at the commit, which comes
before the first Discord call. Everything after that was free to interleave: a superseded close
was watched locking a thread the reopen beside it had just unlocked.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.services.sync.one_at_a_time import _ONE_ITEM_AT_A_TIME, ItemLock, _lock_key

pytestmark = pytest.mark.integration

# Two items that share nothing, including the low bits the key is folded down to.
ONE = 4_242
ANOTHER = 9_999


async def postgres_holds(sessionmaker: async_sessionmaker, item: int) -> bool:
    """Whether Postgres says the lock on that item is held by anybody at all.

    Asked of the database rather than of this process, because taking the lock again from here
    is no evidence of anything. `asyncio.wait_for` runs what it is given inline rather than in a
    task of its own, so a key this task is still recorded as holding would answer instead of the
    database; and the pool is likely to hand back the very connection the block just used, which
    Postgres grants the lock to again for nothing, a session already holding one being welcome
    to it. A test written that way cannot tell "let go of" from "still ours".
    """
    async with sessionmaker() as session:
        held = await session.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = :first AND objid = :second AND objsubid = 2 AND granted"
            ),
            {"first": _ONE_ITEM_AT_A_TIME, "second": _lock_key(item) & 0xFFFF_FFFF},
        )
    return bool(held)


async def warm_the_pool(sessionmaker: async_sessionmaker) -> None:
    """Open the connections the test is about to need, before it starts timing anything.

    The tier hands back a pool with one connection in it, so one of two writers gathered below
    pays for a fresh asyncpg handshake. That is not what any of these tests mean to measure, and
    it is the difference between a second being plenty and a second being a deadline.
    """
    async with sessionmaker() as first, sessionmaker() as second:
        await asyncio.gather(first.execute(text("SELECT 1")), second.execute(text("SELECT 1")))


class _Door:
    """Records who is inside, and holds the door open for whoever is meant to arrive next.

    The wait is what gives an overlap room to happen. Without it two writers can take turns on
    nothing but how the awaits happened to land, and a test written that way passes whether the
    lock is there or not.

    It has to give up rather than hang, and how long it waits depends on which answer is the one
    under test. Where the lock is expected to hold, giving up is the passing case and a short
    wait only costs the test that long. Where the two are expected to get in together, giving up
    is the failure, so the wait is long enough that only a real lock can cause it.
    """

    def __init__(self, wait: float) -> None:
        self.inside: list[str] = []
        self._wait = wait
        self._joined = asyncio.Event()
        self._entered = 0

    async def through(self, lock: ItemLock, item: int) -> None:
        async with lock.held(item):
            self.inside.append("in")
            self._entered += 1
            if self._entered > 1:
                self._joined.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._joined.wait(), timeout=self._wait)
            self.inside.append("out")


async def test_two_writers_of_one_item_do_not_overlap(
    db_sessionmaker: async_sessionmaker,
) -> None:
    await warm_the_pool(db_sessionmaker)
    lock = ItemLock(db_sessionmaker)
    door = _Door(wait=1)

    await asyncio.gather(door.through(lock, ONE), door.through(lock, ONE))

    assert door.inside == ["in", "out", "in", "out"], (
        f"both writers were inside at once: {door.inside}"
    )


async def test_two_different_items_do_not_wait_for_each_other(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """The other half of it, and the half a lock taken on nothing in particular would fail.

    Every delivery this bot handles passes through here, so serialising them all would put the
    whole queue behind whichever item is slowest to answer.
    """
    await warm_the_pool(db_sessionmaker)
    lock = ItemLock(db_sessionmaker)
    door = _Door(wait=5)

    await asyncio.gather(door.through(lock, ONE), door.through(lock, ANOTHER))

    assert door.inside == ["in", "in", "out", "out"], (
        f"a writer of one item waited on a writer of another: {door.inside}"
    )


async def test_taking_it_again_inside_is_let_straight_through(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """`/set_status` holds an item and re-renders it through the ordinary sync, which takes this
    for itself. Without letting the second one through, that is one caller waiting on itself for
    ever, and it is not hypothetical: it stopped the whole integration tier once already.

    Two objects rather than one, because two is what production nests. The workflow holds its
    own and the sync service holds another, and the only thing that lets the inner one see the
    outer one's hold is that the record of it belongs to the module rather than to an instance.
    Nested on a single object this passes either way, and moving that record onto the instance
    is the obvious tidy-up for a module global.
    """
    outer = ItemLock(db_sessionmaker)
    inner = ItemLock(db_sessionmaker)

    async def one_inside_the_other() -> None:
        async with outer.held(ONE), inner.held(ONE):
            pass

    await asyncio.wait_for(one_inside_the_other(), timeout=10)


async def test_it_is_let_go_of_when_the_block_ends(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """Nothing releases it by name, so this is the whole of the release: the transaction it was
    taken in ends, and the connection is rolled back before anything else is given it."""
    lock = ItemLock(db_sessionmaker)

    async with lock.held(ONE):
        assert await postgres_holds(db_sessionmaker, ONE), "it was never taken to begin with"

    assert not await postgres_holds(db_sessionmaker, ONE)


async def test_an_exception_inside_still_lets_it_go(
    db_sessionmaker: async_sessionmaker,
) -> None:
    lock = ItemLock(db_sessionmaker)

    with pytest.raises(RuntimeError):
        async with lock.held(ONE):
            raise RuntimeError("the Discord call this was holding the item for")

    assert not await postgres_holds(db_sessionmaker, ONE)


async def test_the_note_of_holding_it_is_given_back_after_a_failure(
    db_sessionmaker: async_sessionmaker,
) -> None:
    """Not the lock itself, the note this process keeps of holding it, which is what lets a
    caller take it again without waiting.

    A note left behind is worse than a lock left behind, because nothing about it looks wrong.
    The worker handles delivery after delivery in one task, and a sync raising part way through
    is routine, so one refusal would leave every later sync of that item walking straight past a
    lock it never took, for the life of the process.
    """
    lock = ItemLock(db_sessionmaker)
    with pytest.raises(RuntimeError):
        async with lock.held(ONE):
            raise RuntimeError("a Discord refusal, which is an ordinary Tuesday")

    async with lock.held(ONE):
        assert await postgres_holds(db_sessionmaker, ONE), (
            "the second hold took nothing, so the first one's note was never given back"
        )
