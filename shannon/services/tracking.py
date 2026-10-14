"""Reading and changing which lines a thread is told. Issue #163.

The service behind the command: it finds the item a thread belongs to and either reads what that
thread chose or writes down one more choice. Deciding whether a line is posted happens where each
line is posted, from the row each of those already has; this only keeps the answer.

Found by its thread rather than through `workflow.locate`, which names only pull requests and
issues in its refusal, reads a repository nothing here needs, and does not hold the row. Choosing
holds it, so whether a kind was already on is read against what is about to be written.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.tracking import HAPPENS_TO, TrackedEvent, Tracking
from shannon.services.workflow import NotAnItemThreadError

logger = logging.getLogger(__name__)

NOT_AN_ITEM_THREAD = (
    "Run this inside the thread of a pull request, issue or board card this bot is tracking."
)


@dataclass(frozen=True, slots=True)
class ThreadLines:
    """What one thread is told, read back for whoever asked."""

    kind: ObjectType
    tracking: Tracking
    # Whether the kind just chosen was on before the choice, so the answer can say "already".
    # None for a read, and for a kind this item never gets, which nothing was written for.
    was: bool | None = None


class ThreadTracking:
    """Reads and writes which lines an item's thread is told."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def told(self, *, thread_id: int) -> ThreadLines:
        """What the thread is told now: its own choices, and the defaults for the rest."""
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_thread(thread_id)
        if item is None:
            raise NotAnItemThreadError(NOT_AN_ITEM_THREAD)
        return ThreadLines(kind=item.github_object_type, tracking=Tracking.of(item.tracking))

    async def choose(
        self, *, thread_id: int, event: TrackedEvent, wanted: bool, by: int
    ) -> ThreadLines:
        """Turn one kind of line on or off for the thread, answering what it is told after.

        Written even when it was already so: a thread that said "on" keeps saying it if a later
        release changes the default. A kind this item never gets - commits on an issue - writes
        nothing and comes back with `was` unset, for the caller to refuse.
        """
        async with self._sessionmaker() as session, session.begin():
            store = TrackedItemStore(session)
            item = await store.get_by_thread(thread_id, lock=True)
            if item is None:
                raise NotAnItemThreadError(NOT_AN_ITEM_THREAD)
            kind = item.github_object_type
            before = Tracking.of(item.tracking)
            if event not in HAPPENS_TO[kind]:
                return ThreadLines(kind=kind, tracking=before)
            chosen = await store.remember_tracking(item.id, event=event, wanted=wanted)
        logger.info(
            "%s turned %s %s in thread %s",
            by,
            event.value,
            "on" if wanted else "off",
            thread_id,
        )
        return ThreadLines(kind=kind, tracking=Tracking.of(chosen), was=before.wants(event))
