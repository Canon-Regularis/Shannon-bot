"""`/regenerate`: redraw this thread's item from GitHub.

For the threads nothing else reaches: a closed pull request gains labels and assignees with no
delivery ever coming to say so, and somebody who linked their GitHub account after its thread was
opened stays named in plain text in it until something redraws it. `/refresh` has redrawn every
open item's thread since issue #185, so this is for a closed one, and for one thread right now.

One run per thread at a time, and the next five seconds after the last finished, which issue #185
asked for: a redraw is a GitHub read and a Discord write, and asking for one over and over buys
nothing the first one did not.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._cooldowns import Cooldown, NotYet, seconds_in_words
from shannon.commands._guards import in_a_thread, its_turn
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.sync.regenerate import RegenerateOutcome

logger = logging.getLogger(__name__)

# The same sentence `/refresh` ends on, and for the same reason since issue #185: both resolve
# everybody and then tell Discord to ring none of them.
_QUIET = "Nobody was pinged."

# One run per thread at a time, and the next this long after the last finished. Issue #185.
REGENERATE_SPACING_SECONDS = 5.0


class RedrawsAnItem(Protocol):
    """Reading one item from GitHub again and rewriting the block in its thread."""

    async def regenerate(self, *, thread_id: int) -> RegenerateOutcome: ...


def build_regenerate_command(
    service: RedrawsAnItem, gate: PermissionGate, *, cooldown: Cooldown | None = None
) -> SlashCommand:
    # Built here unless a test hands one in, so the one the container builds lasts the process.
    turns = cooldown if cooldown is not None else Cooldown(REGENERATE_SPACING_SECONDS)

    @app_commands.command(
        name="regenerate",
        description="Redraw this item's details from GitHub, without pinging anybody",
    )
    @app_commands.guild_only()
    async def regenerate(interaction: discord.Interaction) -> None:
        where = await in_a_thread(interaction, "regenerate", gate, SYNC_ROLES)
        if where is None:
            return

        # Keyed on the thread, which inside one is Discord's `channel_id`.
        if not await its_turn(interaction, turns, where.channel_id, _not_yet):
            return
        # Every way out finishes the run, a failure included, as `/refresh` does and for its
        # reasons.
        try:
            await defer(interaction)
            try:
                # Inside a thread Discord's `channel_id` is the thread's own id, which is why the
                # command takes no argument.
                outcome = await service.regenerate(thread_id=where.channel_id)
            except ShannonError as error:
                logger.warning("/regenerate could not finish: %s", error.message)
                # No `noun`, so a 404 reads as "that item": the command does not know which kind
                # of item it is in until it has looked.
                await reply(interaction, reply_for(error))
            else:
                await reply(interaction, done(_said(outcome)))
        finally:
            turns.finish(where.channel_id)

    # `app_commands.command()` leaves the command's binding type unknown, which
    # `discord_bot/slash.py` argues `Any` is the only truthful thing to put in.
    return regenerate  # pyright: ignore[reportUnknownVariableType]


def _not_yet(wait: NotYet) -> str:
    """Why a redraw may not start yet, for the amber answer `its_turn` gives."""
    if wait.running:
        return (
            "/regenerate is already redrawing this thread. Try again a few seconds after it "
            "finishes."
        )
    return (
        "/regenerate ran in this thread a moment ago. Try again in "
        f"{seconds_in_words(wait.seconds)}."
    )


def _said(outcome: RegenerateOutcome) -> str:
    """What the redraw did, and the two things about it that are surprises.

    A replacement thread means the link is not the thread the command was run in. Writing to an
    archived thread wakes it, so a shut this bot could not put back leaves a finished item's
    thread open in the channel where it had been closed.
    """
    item = f"{outcome.full_name}#{outcome.number}"
    if outcome.created:
        said = f"The thread for {item} had gone, so it has a new one: <#{outcome.thread_id}>."
    else:
        said = f"Redrew {item} from GitHub: <#{outcome.thread_id}>."

    if outcome.shut_refused:
        said += (
            " This thread could not be closed again afterwards, which is usually the bot "
            "missing Manage Threads."
        )
    return f"{said} {_QUIET}"
