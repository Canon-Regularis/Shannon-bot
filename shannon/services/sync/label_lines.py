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

    The block above it already lists every label and is rewritten on every delivery, so this
    tells the reader nothing new. What it does is make the change visible: Discord posts no
    message when a message is edited, notifies nobody, and does not bump the thread, so tagging
    an item changed the block and looked from the channel exactly like nothing happening.

    Its own class rather than a branch inside the sync, because the sync is about bringing the
    thread into line with a snapshot and this is about announcing one delivery. The sync runs
    for every event and for `/pr` and the board; this runs for two actions and only from a
    webhook.

    It reads the delivery itself rather than being handed a move, so the item handler does not
    have to know that labels exist. Every announcer on that seam owns its own gate.
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

        Keyed on the delivery rather than on the label, because the delivery is what repeats. The
        same label can legitimately go on, come off and go on again, and each of those is a
        separate thing to say; only the same delivery arriving twice is not. GitHub's Redeliver
        button reuses the delivery id and the queue revives that same row, so a redelivery keys
        the same and is turned away there too.

        Said even where the sync turned the delivery away as superseded, which is the opposite of
        what the state line does and is right for the opposite reason. A label going on is a fact
        about this delivery and not a claim about the item's current shape, and a retry of a
        delivery whose line was never posted is exactly the superseded case.
        """
        move = parse_label_move(arrival.action, arrival.payload)
        if move is None:
            return

        if move.added and await self._already_shown(arrival.tracked_item_id, move.name):
            # An item opened with labels already on it is not one delivery: GitHub fires `opened`
            # and a `labeled` for each of them together. The block that went up a moment ago
            # listed every one, so saying they were added is telling a reader what they are
            # looking at. Issue #81.
            #
            # Read AFTER the sync, which is where the handler calls this and is what makes the
            # answer the same either way round. If the `labeled` delivery is the one that opened
            # the thread, its own posted block recorded the name and this read sees it; if
            # `opened` got there first, it sees the set that block wrote.
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
        # Only once the line has actually landed. A refused post hands its claim back, and the
        # retry has to be able to say the same thing.
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
