"""Saying out loud that a label moved, which the metadata block cannot do."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.mirrored_notes import MirroredNoteStore
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.models import LabelMove

logger = logging.getLogger(__name__)

Renderer = Callable[[LabelMove], str]


class LabelLine:
    """Posts one line into an item's thread when a label goes on or comes off.

    The block above it already lists every label and is rewritten on every delivery, so this
    tells the reader nothing new. What it does is make the change visible: Discord posts no
    message when a message is edited, notifies nobody, and does not bump the thread, so tagging
    an item changed the block and looked from the channel exactly like nothing happening.

    Its own class rather than a branch inside the sync, because the sync is about bringing the
    thread into line with a snapshot and this is about announcing one delivery. The sync runs
    for every event and for `/pr` and the board; this runs for two actions and only from a
    webhook.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: PostsToThread,
        *,
        render: Renderer,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._render = render

    async def say(
        self, *, tracked_item_id: int, thread_id: int, move: LabelMove, arrived: int
    ) -> None:
        """Announce the move, once, however many times this delivery is handled.

        Claimed before the post and not recorded after it, for the reason the note mirror gives:
        the queue is at-least-once by design, a delivery whose status could not be written comes
        back when its lease runs out and is handled again from the top, and recording afterwards
        leaves that same gap one step further along.

        Keyed on the delivery rather than on the label, because the delivery is what repeats. The
        same label can legitimately go on, come off and go on again, and each of those is a
        separate thing to say; only the same delivery arriving twice is not. GitHub's Redeliver
        button reuses the delivery id and the queue revives that same row, so a redelivery keys
        the same and is turned away here too.
        """
        note_key = f"label:{arrived}"
        if not await self._claim(tracked_item_id, note_key):
            logger.info(
                "delivery %s has already been announced on tracked item %s",
                arrived,
                tracked_item_id,
            )
            return

        try:
            await self._threads.post(thread_id=thread_id, content=self._render(move))
        except BaseException:
            # Nothing was said, so the claim goes back or the retry reads it as already announced
            # and the line is lost. Cancellation counts as a failure here for the reason the note
            # mirror catches everything: the worker puts a deadline on each delivery and cancels
            # the handler where it stands, and discord.py sleeps through a rate limit rather than
            # failing, so where it stands is often exactly here.
            await self._hand_back(tracked_item_id, note_key)
            raise

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
                "could not give back the claim on %s for tracked item %s, so the line saying a "
                "tag moved is recorded as posted and never was; remove that row from "
                "mirrored_notes to have it said",
                note_key,
                tracked_item_id,
            )

    async def _release(self, tracked_item_id: int, note_key: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await MirroredNoteStore(session).release(tracked_item_id, note_key)
