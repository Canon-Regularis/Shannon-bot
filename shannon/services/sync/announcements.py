"""One line posted into an item's thread, because a rewritten block cannot show a change.

Discord sends no message when a message is edited: it notifies nobody and does not bump the
thread, so a delivery that only rewrites the metadata block looks from the channel like nothing
happened. Posting is the one step on this path that is not repeatable, hence the claim.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import Notify, PostsToThread
from shannon.domain.json import JsonObject
from shannon.domain.models import TrackedSnapshot
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Arrival:
    """One delivery that reached a thread, for whatever wants to say something about it.

    Not the queue's `Delivery`, which is the row this was read from. Both the payload and the
    snapshot, because the two announcers need different halves: which label moved is named only
    at the top level of the raw delivery, and what a closed pull request did is reconciled once
    on the snapshot. The sync's outcome is deliberately absent, because the tag line posts on a
    superseded delivery on purpose and the state line asks the item's row instead.
    """

    # What GitHub called this delivery. Read here rather than off the snapshot, whose copy is
    # None for a sync driven by a command or the board.
    action: str
    snapshot: TrackedSnapshot
    payload: JsonObject
    tracked_item_id: int
    thread_id: int
    # The number the queue gave this delivery. Every announcement is keyed on it, because the
    # delivery is the thing that repeats.
    arrived: int
    # Whether a permission refused to shut the thread on this delivery. Carried because the row
    # cannot say it: a thread nobody asked to shut and one Discord would not let this bot shut
    # both leave the column reading open.
    shut_refused: bool = False


class AnnouncesInThread(Protocol):
    """Saying one thing about a delivery, or saying nothing, which is the usual answer.

    Each announcer holds its own gate: one reads the payload for a label, the other reads the row
    to find out whether the item still agrees with what the delivery says about it.
    """

    async def say(self, arrival: Arrival) -> None: ...


class ClaimedLine:
    """Posts one line into a thread, once, however many times its delivery is handled."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._shut_again = shut_again

    async def say_once(
        self,
        *,
        tracked_item_id: int,
        thread_id: int,
        note_key: str,
        panel: Panel,
        notify: Notify = None,
    ) -> None:
        """Claim the line, post it, and give the claim back if the post did not land.

        Claimed before the post, not recorded after it: the queue is at-least-once, and a delivery
        whose status could not be written is handled again from the top when its lease runs out.
        `notify` permits a notification rather than producing one, and `_person` still needs an
        account map to build a mention, so a renderer handed none cannot ping anybody.
        """
        if not await self._claim(tracked_item_id, note_key):
            logger.info(
                "%s has already been announced on tracked item %s", note_key, tracked_item_id
            )
            return

        try:
            await self._threads.post(thread_id=thread_id, panel=panel, notify=notify)
        except BaseException:
            # Nothing was said, so the claim goes back or the retry reads it as already
            # announced and the line is lost. Cancellation counts as a failure: the worker puts a
            # deadline on each delivery, and discord.py sleeps through a rate limit rather than
            # failing.
            await self._hand_back(tracked_item_id, note_key)
            raise

        # Posting reopened the thread, because Discord will not take a message into an archived
        # one. The closing header lands here a moment after the sync shut the thread it is
        # describing, so without this the thread the header calls closed is open.
        await self._shut_again.again(tracked_item_id=tracked_item_id, thread_id=thread_id)

    async def _claim(self, tracked_item_id: int, note_key: str) -> bool:
        async with self._sessionmaker() as session, session.begin():
            return await MirroredNoteStore(session).claim(tracked_item_id, note_key)

    async def _hand_back(self, tracked_item_id: int, note_key: str) -> None:
        """Give the claim back, shielded, and say so loudly if even that cannot be done.

        Shielded because the usual reason for being here is the delivery's deadline expiring, and
        an unshielded release would be cancelled at its first await. The failure that brought us
        here is the one worth raising, so this one is swallowed.
        """
        try:
            await asyncio.shield(self._release(tracked_item_id, note_key))
        except Exception:
            logger.error(
                "could not give back the claim on %s for tracked item %s, so a line that was "
                "never posted is recorded as posted; remove that row from mirrored_notes to "
                "have it said",
                note_key,
                tracked_item_id,
            )

    async def _release(self, tracked_item_id: int, note_key: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await MirroredNoteStore(session).release(tracked_item_id, note_key)
