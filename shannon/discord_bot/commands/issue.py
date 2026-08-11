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
from shannon.services.manual_sync import ManualSync, SyncFailedError

logger = logging.getLogger(__name__)


def build_issue_command(service: ManualSync, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(name="issue", description="Sync a GitHub issue into Discord")
    @app_commands.describe(issue_link="Link to the GitHub issue")
    @app_commands.guild_only()
    async def issue(interaction: discord.Interaction, issue_link: str) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, SYNC_ROLES):
            await reply(interaction, gate.denial("issue", SYNC_ROLES))
            return

        await defer(interaction)
        try:
            outcome = await service.sync_link(guild_id=interaction.guild_id, link=issue_link)
        except UnparseableLinkError as error:
            await reply(interaction, f"That link did not work. {error.message}")
        except (NotRegisteredError, RepositoryMismatchError, SyncFailedError) as error:
            await reply(interaction, error.message)
        except GitHubNotFoundError:
            await reply(interaction, "GitHub has no issue at that link.")
        except GitHubError as error:
            logger.warning("/issue failed against GitHub: %s", error.message)
            await reply(interaction, f"GitHub could not be reached. {error.message}")
        except DiscordGatewayError as error:
            logger.warning("/issue failed against Discord: %s", error.message)
            await reply(interaction, f"Discord refused the update. {error.message}")
        else:
            verb = "Opened" if outcome.created else "Updated"
            await reply(
                interaction,
                f"{verb} the thread for {outcome.full_name}#{outcome.number}: "
                f"<#{outcome.thread_id}>",
            )

    return issue
