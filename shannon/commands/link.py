from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.services.linking import InvalidGitHubUsernameError

logger = logging.getLogger(__name__)


class LinksAccounts(Protocol):
    """Binding a GitHub login to a Discord account."""

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str: ...


def build_link_command(service: LinksAccounts, gate: PermissionGate) -> app_commands.Command:
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
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return

        # Every link, not only the ones made on somebody else's behalf.
        #
        # Claiming your own account used to be ungated, on the reasoning that it is yours to
        # claim. Nothing checked that it was: GitHub is never asked, so anybody could take any
        # login and from then on receive every mention meant for it in this server, in the
        # metadata block and in every ping. That is the same route by which somebody could have
        # become a review team before teams were given a table of their own, and the honest fix
        # is the same one, which is that a person who speaks for the server does the pointing.
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("link", REGISTER_ROLES))
            return

        target = member or interaction.user

        await defer(interaction)
        try:
            username = await service.link(
                guild_id=interaction.guild_id,
                github_username=github_username,
                discord_user_id=target.id,
            )
        except InvalidGitHubUsernameError as error:
            await reply(interaction, error.message)
        else:
            await reply(interaction, f"Linked GitHub user {username} to <@{target.id}>.")

    return link
