"""The checks a slash command makes before it does anything.

`None` means the guard has already answered the interaction and the caller must return.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

import discord

from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import refused, reply
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
        await reply(interaction, refused(NOT_IN_A_SERVER))
        return None
    if not gate.allows(interaction.user, roles):
        await reply(interaction, refused(gate.denial(command, roles)))
        return None
    return interaction.guild_id


async def in_a_thread(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
) -> InAThread | None:
    """The guild and channel a permitted caller ran this in, or None having said why not.

    The channel is checked after the tier, so somebody without the role is told that instead.
    """
    guild_id = await in_a_server(interaction, command, gate, roles)
    if guild_id is None:
        return None
    if interaction.channel_id is None:
        await reply(interaction, refused(NOT_IN_A_THREAD))
        return None
    return InAThread(guild_id=guild_id, channel_id=interaction.channel_id)


class DecidesGitHubAccess(Protocol):
    """Whether the caller's GitHub account may make this change, and why not where it may not."""

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, at_least: str
    ) -> str | None: ...


async def github_allows(
    interaction: discord.Interaction,
    access: DecidesGitHubAccess,
    guild_id: int,
    *,
    at_least: str,
) -> bool:
    """Whether to go on, having already said why not where the answer is no.

    Called AFTER the interaction is deferred, never before. Discord allows three seconds for a
    first response and this makes a network call, so asking first risks an interaction that
    expires rather than one that refuses - and an expired interaction says nothing at all, which
    is the one outcome worse than either answer.

    The role check comes first, so somebody without the Discord role is told that rather than
    being told about a GitHub account they may not have connected.
    """
    refusal = await access.refusal_for(
        guild_id=guild_id, discord_user_id=interaction.user.id, at_least=at_least
    )
    if refusal is None:
        return True
    await reply(interaction, refused(refusal))
    return False
