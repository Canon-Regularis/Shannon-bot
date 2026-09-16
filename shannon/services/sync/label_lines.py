"""Saying out loud that a label moved, which the metadata block cannot do."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.discord_bot.threads import PostsToThread
from shannon.domain.models import LabelMove
from shannon.github.webhooks.labels import parse_label_move
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

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

    It reads the delivery itself rather than being handed a move, so the item handler does not
    have to know that labels exist. Every announcer on that seam owns its own gate.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: PostsToThread,
        *,
        render: Renderer,
        shut_again: KeepsThreadsShut,
    ) -> None:
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

        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=f"label:{arrival.arrived}",
            content=self._render(move),
        )
