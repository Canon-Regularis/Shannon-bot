"""One line posted into an item's thread, because a rewritten block cannot show a change.

Discord posts no message when a message is edited. It notifies nobody and it does not bump the
thread, so everything that only moves the metadata block looks from the channel exactly like
nothing happening. An announcer answers that for one kind of change: it decides whether this
delivery is one it has anything to say about, and hands the words to `ClaimedLine`.

The claim is what makes it safe to say. The delivery queue is at-least-once by design, so every
other handler on this path is written to be repeatable, and posting a message is the one thing
that is not repeatable on its own.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.json import JsonObject
from shannon.domain.models import TrackedSnapshot
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Arrival:
    """One delivery that reached a thread, for whatever wants to say something about it.

    Not the queue's `Delivery`, which is the row this was read from. That one is a payload
    waiting to be worked; this is what it turned into once the sync had found the item, opened
    or claimed its thread, and written the block.

    The payload as well as the snapshot, because the two announcers need different halves. Which
    label moved is named only at the top level of the raw delivery; what a closed pull request
    did is already worked out on the snapshot, where the merged flag and the merged timestamp
    have been reconciled once. Carrying both costs two references the handler already holds.

    What is deliberately absent is the sync's outcome. Neither announcer reads it: the tag line
    posts on a superseded delivery on purpose, and the state line asks the item's row instead,
    which is a better question for the reason `StateLine` gives at length.
    """

    # What GitHub called this delivery. Taken from the delivery rather than from the copy on the
    # snapshot, which is optional because a sync driven by a command or the board has no action
    # at all, so reading it there would make every announcer handle a None that cannot happen.
    action: str
    snapshot: TrackedSnapshot
    payload: JsonObject
    tracked_item_id: int
    thread_id: int
    # The number the queue gave this delivery, which is the order it reached this bot. It is what
    # every announcement is keyed on, because the delivery is the thing that repeats.
    arrived: int
    # Whether a permission refused to shut the thread on this delivery. The one thing here that
    # is the sync's outcome rather than the delivery's, and it is carried because the row cannot
    # say it: a thread nobody asked to shut and one Discord would not let this bot shut both
    # leave the column reading open, and only one of them is worth a line in the thread.
    shut_refused: bool = False


class AnnouncesInThread(Protocol):
    """Saying one thing about a delivery, or saying nothing, which is the usual answer.

    Each announcer holds its own gate rather than being handed a filtered delivery, because the
    gates have nothing in common: one reads the payload for a label, the other reads the row to
    find out whether the item still agrees with what the delivery says about it.
    """

    async def say(self, arrival: Arrival) -> None: ...


class ClaimedLine:
    """Posts one line into a thread, once, however many times its delivery is handled.

    Shared by both announcers rather than copied into each, because the interesting part of an
    announcer is its gate and this is the part that is identical. The failure paths are the
    other half of the argument: the last of them is only reached when a Discord failure and a
    database failure arrive together, so a second copy would need its own test for that to keep
    the coverage floor, and that test would prove nothing the first one has not.
    """

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
        self, *, tracked_item_id: int, thread_id: int, note_key: str, content: str
    ) -> None:
        """Claim the line, post it, and give the claim back if the post did not land.

        Claimed before the post and not recorded after it, for the reason the note mirror gives:
        the queue is at-least-once by design, a delivery whose status could not be written comes
        back when its lease runs out and is handled again from the top, and recording afterwards
        leaves that same gap one step further along.
        """
        if not await self._claim(tracked_item_id, note_key):
            logger.info(
                "%s has already been announced on tracked item %s", note_key, tracked_item_id
            )
            return

        try:
            await self._threads.post(thread_id=thread_id, content=content)
        except BaseException:
            # Nothing was said, so the claim goes back or the retry reads it as already announced
            # and the line is lost. Cancellation counts as a failure here for the reason the note
            # mirror catches everything: the worker puts a deadline on each delivery and cancels
            # the handler where it stands, and discord.py sleeps through a rate limit rather than
            # failing, so where it stands is often exactly here.
            await self._hand_back(tracked_item_id, note_key)
            raise

        # Posting reopened the thread, because Discord will not take a message into an archived
        # one. This is where the closing header lands, a moment after the sync shut the thread
        # it is describing, so without this the thread the header says is closed is open.
        #
        # Required rather than optional, and that is deliberate. A collaborator that can be
        # left out is one every test leaves out, and what it would be hiding is a feature that
        # silently does nothing.
        await self._shut_again.again(tracked_item_id=tracked_item_id, thread_id=thread_id)

    async def _claim(self, tracked_item_id: int, note_key: str) -> bool:
        async with self._sessionmaker() as session, session.begin():
            return await MirroredNoteStore(session).claim(tracked_item_id, note_key)

    async def _hand_back(self, tracked_item_id: int, note_key: str) -> None:
        """Give the claim back, shielded, and say so loudly if even that cannot be done.

        Shielded because the usual reason for being here is the delivery's deadline expiring,
        and an unshielded release would be cancelled at its first await for the same reason the
        post was. Swallowed because the failure that brought us here is the one worth raising.
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
