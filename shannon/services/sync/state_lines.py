"""Saying out loud that an item closed, merged or reopened."""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import StateChange
from shannon.domain.state_changes import state_change_of
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)


class Renderer(Protocol):
    """The words, which are the one thing this does not decide.

    A protocol rather than a `Callable` alias, because `shut` is keyword only and a `Callable`
    cannot say so. The disagreement would show up nowhere until a TypeError inside a Discord
    phase failed the delivery, on every retry.
    """

    def __call__(self, change: StateChange, *, shut: bool, refused: bool = False) -> Panel: ...


class StateLine:
    """Posts a header into an item's thread when the item closes, merges or reopens.

    Discord says nothing when a block is rewritten or a thread is shut, and command replies here
    are ephemeral, so otherwise an item could close and shut the discussion under it unseen.
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
        """Announce the move, unless the item has moved on from what this delivery says.

        The gate is the row rather than the sync's outcome, which matters in three cases. A close
        overtaken by a reopen that landed first would announce a close under a block reading Open.
        A close superseded by a later `edited` delivery carrying `closed` would lose its line for
        good, because an `edited` is not a state move and a closed item sends no further events.
        A retry of a delivery whose post failed finds the row already carrying its own state, so
        the line it is owed is still said. What is left is the window between this read and the
        post, which costs at most one surplus line under a block that is correct.
        """
        change = state_change_of(arrival.action, arrival.snapshot)
        if change is None:
            return

        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(arrival.tracked_item_id)

        # The row cannot be missing here, since the delivery only reached a thread by way of
        # it. Checked anyway, because the cost of being wrong is an attribute read on None inside
        # a Discord call.
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
            panel=self._render(
                change,
                shut=item.discord_thread_locked is True,
                refused=arrival.shut_refused,
            ),
        )
