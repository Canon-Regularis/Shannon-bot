from __future__ import annotations

import logging

import discord
from discord import app_commands

from shannon.discord_bot.commands._replies import reply_for
from shannon.discord_bot.permissions import SYNC_ROLES, PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.errors import ShannonError
from shannon.services.manual_sync import ManualSync

logger = logging.getLogger(__name__)


async def run_sync_link(
    interaction: discord.Interaction,
    link: str,
    *,
    name: str,
    noun: str,
    service: ManualSync,
    gate: PermissionGate,
) -> None:
    """Everything /pr and /issue do; the two differ only in what they are called.

    Each keeps its own parameter name, because that is what somebody types in Discord.
    """
    if interaction.guild_id is None:
        await reply(interaction, "Run this inside a server channel.")
        return
    if not gate.allows(interaction.user, SYNC_ROLES):
        await reply(interaction, gate.denial(name, SYNC_ROLES))
        return

    await defer(interaction)
    try:
        outcome = await service.sync_link(guild_id=interaction.guild_id, link=link)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error, noun=noun))
    else:
        verb = "Opened" if outcome.created else "Updated"
        await reply(
            interaction,
            f"{verb} the thread for {outcome.full_name}#{outcome.number}: <#{outcome.thread_id}>",
        )


def build_pr_command(service: ManualSync, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(name="pr", description="Sync a GitHub pull request into Discord")
    @app_commands.describe(pr_link="Link to the GitHub pull request")
    @app_commands.guild_only()
    async def pr(interaction: discord.Interaction, pr_link: str) -> None:
        await run_sync_link(
            interaction, pr_link, name="pr", noun="pull request", service=service, gate=gate
        )

    return pr


def build_issue_command(service: ManualSync, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(name="issue", description="Sync a GitHub issue into Discord")
    @app_commands.describe(issue_link="Link to the GitHub issue")
    @app_commands.guild_only()
    async def issue(interaction: discord.Interaction, issue_link: str) -> None:
        await run_sync_link(
            interaction, issue_link, name="issue", noun="issue", service=service, gate=gate
        )

    return issue
