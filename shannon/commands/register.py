from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.threads import THREADABLE
from shannon.domain.errors import ShannonError
from shannon.services.registration import RegistrationResult

logger = logging.getLogger(__name__)


class RegistersRepositories(Protocol):
    """Binding a GitHub repository to this server."""

    async def register(
        self, *, guild_id: int, channel_id: int, link: str
    ) -> RegistrationResult: ...


def build_register_command(
    service: RegistersRepositories, gate: PermissionGate
) -> app_commands.Command:
    @app_commands.command(
        name="register", description="Bind a GitHub repository to this Discord server"
    )
    @app_commands.describe(github_repo_link="Link to the GitHub repository")
    @app_commands.guild_only()
    async def register(interaction: discord.Interaction, github_repo_link: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("register", REGISTER_ROLES))
            return
        # The channel this was run in becomes the home for pull request threads, and a thread
        # or a voice channel cannot hold one. Refusing here is the last chance to say so to
        # somebody who is looking; the sync path hits it hours later with nobody to tell.
        if not isinstance(interaction.channel, THREADABLE):
            await reply(
                interaction,
                "Run /register in a text or forum channel. Threads cannot be opened here.",
            )
            return

        await defer(interaction)
        try:
            result = await service.register(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                link=github_repo_link,
            )
        except ShannonError as error:
            # One table for every command, in _replies. Repeating four of its rows here meant
            # two places to keep in step, and the wording had already been copied rather than
            # shared.
            logger.warning("register failed: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(
                interaction,
                f"Registered {result.full_name}. Pull request threads will appear in "
                f"<#{result.pr_channel_id}>.",
            )

    return register
