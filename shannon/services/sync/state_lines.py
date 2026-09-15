"""Saying out loud that an item closed, merged or reopened."""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import StateChange
from shannon.domain.state_changes import state_change_of
from shannon.services.sync.announcements import Arrival, ClaimedLine

logger = logging.getLogger(__name__)


class Renderer(Protocol):
    """The words, which are the one thing this does not decide.

    A protocol rather than a `Callable` alias, because `locked` is keyword only and a `Callable`
    cannot say so. An alias here describes a call nobody makes, and the disagreement does not
    show up in either module: it arrives as a TypeError from inside a Discord phase, which fails
    the delivery and comes back every time it is retried.
    """

    def __call__(self, change: StateChange, *, locked: bool) -> str: ...


class StateLine:
    """Posts a header into an item's thread when the item closes, merges or reopens.

    The same silence the tag line answers, and the loudest case of it. Closing an issue rewrites
    the metadata block and locks the thread, and Discord says nothing about either, so an item
    could close, shut the discussion under it, and leave no trace whatever in the channel. The
    only text anybody saw was the `/set_done` reply, and every command reply here is ephemeral,
    so nobody but the person who ran it ever read one.

    One announcer for both kinds of item. Which of them shuts a thread when it closes is not
    restated here: the row is read for what the thread actually is, which is a better question
    than what this kind of item usually does, and answers a case that restating the policy would
    get wrong.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: PostsToThread,
        *,
        render: Renderer,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._line = ClaimedLine(sessionmaker, threads)
        self._render = render

    async def say(self, arrival: Arrival) -> None:
        """Announce the move, unless the item has moved on from what this delivery says.

        The gate is the row rather than the sync's outcome, and the difference matters in three
        cases that all happen.

        A close overtaken by a reopen that landed first would otherwise announce a close for an
        item that is open, under a block that says Open. The row turns that away.

        A close overtaken by a later `edited` delivery that also carried `closed` is turned away
        as superseded, and that later delivery announces nothing, because an `edited` is not a
        state move. Gating on the outcome would lose the line for good, and a closed item sends
        no further events, so nothing would ever come back for it. The row lets it through,
        because the row agrees.

        And a retry of a delivery whose post failed finds the row already carrying its own
        state, so the line it is owed is still said. Reading a before-and-after off the sync
        would have that retry find nothing changed and say nothing, which is the failure the
        claim being handed back exists to prevent, moved one step earlier where the hand back
        cannot reach it.

        What is left is the width of the window between this read and the post, which is the
        same window the lock accepts, and the cost of losing it is one surplus line under a
        block that is correct.
        """
        change = state_change_of(arrival.action, arrival.snapshot)
        if change is None:
            return

        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(arrival.tracked_item_id)

        # The row cannot be missing here: the delivery only reached a thread by way of that row.
        # Checked with the rest rather than trusted, because the cost of being wrong about it is
        # an attribute read on None inside a Discord call.
        if item is None or item.github_state != arrival.snapshot.display_state:
            logger.info(
                "tracked item %s is not %s any more, so nothing is said about it",
                arrival.tracked_item_id,
                arrival.snapshot.display_state,
            )
            return

        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=f"state:{arrival.arrived}",
            content=self._render(change, locked=item.discord_thread_locked is True),
        )
