"""`/pr` and `/issue`: mirroring the GitHub item a pasted link points at into its thread.

The two are one command under two names, so each keeps the option name somebody types in Discord
(`pr_link`, `issue_link`) and the noun a 404 is worded with, and shares everything else in `_act`.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.sync.manual import ManualSyncOutcome

logger = logging.getLogger(__name__)


class SyncsByLink(Protocol):
    """Mirroring the item a link points at."""

    async def sync_link(self, *, guild_id: int, link: str) -> ManualSyncOutcome: ...


async def _act(
    interaction: discord.Interaction,
    link: str,
    *,
    name: str,
    noun: str,
    service: SyncsByLink,
    gate: PermissionGate,
) -> None:
    """Everything /pr and /issue do; the two differ only in what they are called.

    Each keeps its own parameter name, because that is what somebody types in Discord.
    """
    guild_id = await in_a_server(interaction, name, gate, SYNC_ROLES)
    if guild_id is None:
        return

    await defer(interaction)
    try:
        outcome = await service.sync_link(guild_id=guild_id, link=link)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error, noun=noun))
    else:
        verb = "Opened" if outcome.created else "Updated"
        await reply(
            interaction,
            done(
                f"{verb} the thread for {outcome.full_name}#{outcome.number}: "
                f"<#{outcome.thread_id}>."
            ),
        )


def build_pr_command(service: SyncsByLink, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(name="pr", description="Sync a GitHub pull request into Discord")
    @app_commands.describe(pr_link="Link to the GitHub pull request")
    @app_commands.guild_only()
    async def pr(interaction: discord.Interaction, pr_link: str) -> None:
        await _act(interaction, pr_link, name="pr", noun="pull request", service=service, gate=gate)

    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` argues `Any` is the only truthful thing to put in.
    return pr  # pyright: ignore[reportUnknownVariableType]


def build_issue_command(service: SyncsByLink, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(name="issue", description="Sync a GitHub issue into Discord")
    @app_commands.describe(issue_link="Link to the GitHub issue")
    @app_commands.guild_only()
    async def issue(interaction: discord.Interaction, issue_link: str) -> None:
        await _act(interaction, issue_link, name="issue", noun="issue", service=service, gate=gate)

    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` argues `Any` is the only truthful thing to put in.
    return issue  # pyright: ignore[reportUnknownVariableType]
