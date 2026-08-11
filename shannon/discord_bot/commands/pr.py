from __future__ import annotations

import logging

import discord
from discord import app_commands

from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.permissions import SYNC_ROLES, PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.errors import (
    NotRegisteredError,
    RepositoryMismatchError,
    UnparseableLinkError,
)
from shannon.github.errors import GitHubError, GitHubNotFoundError
from shannon.services.manual_sync import ManualPullRequestSync, SyncFailedError

logger = logging.getLogger(__name__)


def build_pr_command(service: ManualPullRequestSync, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(name="pr", description="Sync a GitHub pull request into Discord")
    @app_commands.describe(pr_link="Link to the GitHub pull request")
    @app_commands.guild_only()
    async def pr(interaction: discord.Interaction, pr_link: str) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, SYNC_ROLES):
            await reply(interaction, gate.denial("pr", SYNC_ROLES))
            return

        await defer(interaction)
        try:
            outcome = await service.sync_link(guild_id=interaction.guild_id, link=pr_link)
        except UnparseableLinkError as error:
            await reply(interaction, f"That link did not work. {error.message}")
        except (NotRegisteredError, RepositoryMismatchError, SyncFailedError) as error:
            await reply(interaction, error.message)
        except GitHubNotFoundError:
            await reply(interaction, "GitHub has no pull request at that link.")
        except GitHubError as error:
            logger.warning("/pr failed against GitHub: %s", error.message)
            await reply(interaction, f"GitHub could not be reached. {error.message}")
        except DiscordGatewayError as error:
            logger.warning("/pr failed against Discord: %s", error.message)
            await reply(interaction, f"Discord refused the update. {error.message}")
        else:
            verb = "Opened" if outcome.created else "Updated"
            await reply(
                interaction,
                f"{verb} the thread for {outcome.full_name}#{outcome.number}: "
                f"<#{outcome.thread_id}>",
            )

    return pr
