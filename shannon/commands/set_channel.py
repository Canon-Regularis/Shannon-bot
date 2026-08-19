from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.threads import THREADABLE
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError
from shannon.services.channels import ChannelAssignment

logger = logging.getLogger(__name__)

# Tickets arrive with GitHub Projects in MVP 4, so only the two live types are offered.
CHOICES = [
    app_commands.Choice(name="pull requests", value=ObjectType.PR.value),
    app_commands.Choice(name="issues", value=ObjectType.ISSUE.value),
]


class MapsChannels(Protocol):
    """Pointing one kind of item at the channel its threads appear in."""

    async def assign(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> ChannelAssignment: ...


def build_set_channel_command(service: MapsChannels, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(
        name="set_channel", description="Choose which channel a kind of GitHub item posts into"
    )
    @app_commands.describe(
        object_type="Which kind of GitHub item", channel="Where its threads should appear"
    )
    @app_commands.choices(object_type=CHOICES)
    @app_commands.guild_only()
    async def set_channel(
        interaction: discord.Interaction,
        object_type: app_commands.Choice[str],
        channel: discord.TextChannel | discord.ForumChannel,
    ) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("set_channel", REGISTER_ROLES))
            return
        if not isinstance(channel, THREADABLE):
            await reply(interaction, f"<#{channel.id}> cannot hold threads.")
            return

        await defer(interaction)
        try:
            assignment = await service.assign(
                guild_id=interaction.guild_id,
                object_type=ObjectType(object_type.value),
                channel_id=channel.id,
            )
        except NotRegisteredError as error:
            await reply(interaction, error.message)
        else:
            # What happens to the threads already open is the thing an admin actually needs to
            # know, and it is not what "moved from" would suggest: Discord cannot move a thread
            # between channels, and every item already tracked keeps the one it has. Only new
            # threads go anywhere different.
            stayed = (
                f" Threads already open stay in <#{assignment.replaced}>."
                if assignment.replaced is not None and assignment.replaced != channel.id
                else ""
            )
            await reply(
                interaction,
                f"{object_type.name.capitalize()} for {assignment.repository_name} will now "
                f"appear in <#{channel.id}>.{stayed}",
            )

    return set_channel
