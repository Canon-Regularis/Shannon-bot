"""The checks a slash command makes before it does anything.

Every command needs a guild, most need a permission tier, and the ones that act on an item need
the thread they were run in. Written once here because thirteen callbacks had it written out,
and the three that drifted apart drifted in the reply text rather than the logic.

`None` means the guard has already answered the interaction and the caller must return.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

import discord

from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import reply
from shannon.discord_bot.roles import CommandRole

NOT_IN_A_SERVER = "Run this inside a server channel."
NOT_IN_A_THREAD = "Run this inside the item's thread."


@dataclass(frozen=True, slots=True)
class InAThread:
    """Where a command was run, once both ids are known to be there."""

    guild_id: int
    channel_id: int


async def in_a_server(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
) -> int | None:
    """The guild a permitted caller ran this in, or None having said why not."""
    if interaction.guild_id is None:
        await reply(interaction, NOT_IN_A_SERVER)
        return None
    if not gate.allows(interaction.user, roles):
        await reply(interaction, gate.denial(command, roles))
        return None
    return interaction.guild_id


async def in_a_thread(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
) -> InAThread | None:
    """The guild and channel a permitted caller ran this in, or None having said why not.

    The channel is checked after the tier, so somebody without the role is told that rather than
    being told where to stand.
    """
    guild_id = await in_a_server(interaction, command, gate, roles)
    if guild_id is None:
        return None
    if interaction.channel_id is None:
        await reply(interaction, NOT_IN_A_THREAD)
        return None
    return InAThread(guild_id=guild_id, channel_id=interaction.channel_id)
