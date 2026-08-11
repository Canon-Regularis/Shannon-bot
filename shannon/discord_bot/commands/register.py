from __future__ import annotations

import logging

import discord
from discord import app_commands

from shannon.discord_bot.permissions import REGISTER_ROLES, PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.errors import DuplicateRegistrationError, UnparseableLinkError
from shannon.github.errors import GitHubError, GitHubNotFoundError
from shannon.services.registration import RepositoryRegistrationService

logger = logging.getLogger(__name__)


def build_register_command(
    service: RepositoryRegistrationService, gate: PermissionGate
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

        await defer(interaction)
        try:
            result = await service.register(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                link=github_repo_link,
            )
        except UnparseableLinkError as error:
            await reply(interaction, f"That link did not work. {error.message}")
        except GitHubNotFoundError:
            await reply(interaction, "GitHub has no repository at that link.")
        except DuplicateRegistrationError as error:
            await reply(interaction, error.message)
        except GitHubError as error:
            logger.warning("register failed against GitHub: %s", error.message)
            await reply(interaction, f"GitHub could not be reached. {error.message}")
        else:
            await reply(
                interaction,
                f"Registered {result.full_name}. Pull request threads will appear in "
                f"<#{result.pr_channel_id}>.",
            )

    return register
