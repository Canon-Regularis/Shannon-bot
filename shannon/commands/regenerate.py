"""`/regenerate`: redraw this thread's item from GitHub.

For the threads nothing else reaches: a closed pull request gains labels and assignees with no
delivery ever coming to say so, and somebody who linked their GitHub account after `/refresh`
opened a thread stays named in plain text in it for ever.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_thread
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.sync.regenerate import RegenerateOutcome

logger = logging.getLogger(__name__)

# The same sentence `/refresh` ends on, for a different reason: this one resolves everybody and
# then tells Discord to ring none of them.
_QUIET = "Nobody was pinged."


class RedrawsAnItem(Protocol):
    """Reading one item from GitHub again and rewriting the block in its thread."""

    async def regenerate(self, *, thread_id: int) -> RegenerateOutcome: ...


def build_regenerate_command(service: RedrawsAnItem, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="regenerate",
        description="Redraw this item's details from GitHub, without pinging anybody",
    )
    @app_commands.guild_only()
    async def regenerate(interaction: discord.Interaction) -> None:
        where = await in_a_thread(interaction, "regenerate", gate, SYNC_ROLES)
        if where is None:
            return

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

    # `app_commands.command()` leaves the command's binding type unknown, which
    # `discord_bot/slash.py` argues `Any` is the only truthful thing to put in.
    return regenerate  # pyright: ignore[reportUnknownVariableType]


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
