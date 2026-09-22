"""Saying out loud that a label moved, which the metadata block cannot do."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.models import LabelMove
from shannon.github.webhooks.labels import parse_label_move
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)

Renderer = Callable[[LabelMove], Panel]


class LabelLine:
    """Posts one line into an item's thread when a label goes on or comes off.

    The block above already lists every label, but Discord posts no message when one is edited,
    notifies nobody and does not bump the thread, so the change is invisible from the channel.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        render: Renderer,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._line = ClaimedLine(sessionmaker, threads, shut_again)
        self._render = render

    async def say(self, arrival: Arrival) -> None:
        """Announce the move, once, however many times this delivery is handled.

        Keyed on the delivery, not the label: the same label may go on, come off and go on again,
        while GitHub's Redeliver button reuses the delivery id. Said even where the sync turned
        the delivery away as superseded, because a label going on is a fact about the delivery
        rather than a claim about the item's current shape.
        """
        move = parse_label_move(arrival.action, arrival.payload)
        if move is None:
            return

        if move.added and await self._already_shown(arrival.tracked_item_id, move.name):
            # GitHub fires `opened` and a `labeled` for each label an item was opened with,
            # and the block posted a moment ago already listed every one. Read after the sync,
            # so the answer is the same whichever of the two deliveries opened the thread.
            logger.info(
                "the block already showed %r on tracked item %s, so nothing is said about it",
                move.name,
                arrival.tracked_item_id,
            )
            return

        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=f"label:{arrival.arrived}",
            panel=self._render(move),
        )
        # Only once the line has landed: a refused post hands its claim back, and the retry
        # has to be able to say the same thing.
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).note_label_announced(
                arrival.tracked_item_id,
                thread_id=arrival.thread_id,
                name=move.name,
                on_it=move.added,
            )

    async def _already_shown(self, tracked_item_id: int, name: str) -> bool:
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
        return item is not None and name in (item.shown_labels or ())
