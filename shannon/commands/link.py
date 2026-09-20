from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import REGISTER_ROLES
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.services.linking import InvalidGitHubUsernameError

logger = logging.getLogger(__name__)


class LinksAccounts(Protocol):
    """Binding a GitHub login to a Discord account."""

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str: ...


def build_link_command(service: LinksAccounts, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(name="link", description="Connect a GitHub username to a Discord account")
    @app_commands.describe(
        github_username="The GitHub username to connect",
        member="Whose Discord account to connect it to (defaults to you)",
    )
    @app_commands.guild_only()
    async def link(
        interaction: discord.Interaction,
        github_username: str,
        member: discord.Member | None = None,
    ) -> None:
        # Gated for every link, not only the ones made on somebody else's behalf: GitHub is
        # never asked to confirm the claim, so an ungated self-link lets anybody take any login
        # and from then on receive every mention meant for it in this server.
        guild_id = await in_a_server(interaction, "link", gate, REGISTER_ROLES)
        if guild_id is None:
            return

        target = member or interaction.user

        await defer(interaction)
        try:
            username = await service.link(
                guild_id=guild_id,
                github_username=github_username,
                discord_user_id=target.id,
            )
        except InvalidGitHubUsernameError as error:
            await reply(interaction, error.message)
        else:
            await reply(interaction, done(f"Linked GitHub user {username} to <@{target.id}>."))

    # `app_commands.command()` leaves the command's binding type unknown; one line here rather
    # than a suppression over the whole file.
    return link  # pyright: ignore[reportUnknownVariableType]
