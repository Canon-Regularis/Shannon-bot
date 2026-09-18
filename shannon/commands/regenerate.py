"""`/regenerate`: redraw this thread's item from GitHub.

Run inside the item's own thread and takes no argument, the way the `/set_*` commands do. Inside a
thread, Discord's `channel_id` IS the thread id, which is what makes that possible.

It is for the threads nothing else reaches: a closed pull request gains labels and assignees after
it closes and no delivery ever comes to say so, and somebody who linked their GitHub account after
`/refresh` opened a thread stays named in plain text in it for ever.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.errors import ShannonError
from shannon.services.sync.regenerate import RegenerateOutcome

logger = logging.getLogger(__name__)

# The same sentence `/refresh` ends on, and true here for a different reason: that one resolves
# nobody, and this one resolves everybody and tells Discord to ring none of them.
_QUIET = "Nobody was pinged."


class RedrawsAnItem(Protocol):
    """Reading one item from GitHub again and rewriting the block in its thread."""

    async def regenerate(self, *, thread_id: int) -> RegenerateOutcome: ...


def build_regenerate_command(service: RedrawsAnItem, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(
        name="regenerate",
        description="Redraw this item's details from GitHub, without pinging anybody",
    )
    @app_commands.guild_only()
    async def regenerate(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, SYNC_ROLES):
            await reply(interaction, gate.denial("regenerate", SYNC_ROLES))
            return
        if interaction.channel_id is None:
            await reply(interaction, "Run this inside the item's thread.")
            return

        await defer(interaction)
        try:
            # Inside a thread this is the thread's own id, which is why the command needs no
            # argument. The `/set_*` commands read it the same way.
            outcome = await service.regenerate(thread_id=interaction.channel_id)
        except ShannonError as error:
            logger.warning("/regenerate could not finish: %s", error.message)
            # No `noun`, so a 404 reads as "that item" rather than naming a kind. The command
            # takes no link and does not know which kind it is in until it has looked.
            await reply(interaction, reply_for(error))
        else:
            await reply(interaction, _said(outcome))

    return regenerate


def _said(outcome: RegenerateOutcome) -> str:
    """What the redraw did, and the two things about it that are surprises.

    A replacement thread means the link somebody is about to click is not the thread they ran the
    command in, so it has to be said rather than left to be noticed.

    A refused shut matters more here than anywhere else in the project. Writing to an archived
    thread wakes it, so a shut this bot could not put back leaves a finished item's thread open in
    the channel where it had been closed, which is worse than before the command was run.
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
