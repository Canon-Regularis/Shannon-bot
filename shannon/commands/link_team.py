from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.errors import ShannonError

logger = logging.getLogger(__name__)


class LinksTeams(Protocol):
    """Binding a GitHub team to a Discord role."""

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> str: ...


def build_link_team_command(service: LinksTeams, gate: PermissionGate) -> app_commands.Command:
    @app_commands.command(name="link_team", description="Connect a GitHub team to a Discord role")
    @app_commands.describe(
        github_team="The GitHub team, as it appears in the URL",
        role="The Discord role to ping when that team is asked for a review",
    )
    @app_commands.guild_only()
    async def link_team(
        interaction: discord.Interaction, github_team: str, role: discord.Role
    ) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        # Nobody speaks for a team the way they speak for themselves, so unlike /link there is no
        # anybody-may-claim-their-own case: this points a whole role at a name, which is the same
        # kind of decision as mapping a channel.
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("link_team", REGISTER_ROLES))
            return
        if role.is_default():
            # `@everyone` is a role Discord gives every member, and pinging it is the thing the
            # mention rules in the client exist to make impossible.
            await reply(interaction, "Pick a real role. Everyone is not a review team.")
            return

        await defer(interaction)
        try:
            linked = await service.link(
                guild_id=interaction.guild_id,
                github_team=github_team,
                discord_role_id=role.id,
            )
        except ShannonError as error:
            logger.warning("/link_team could not finish: %s", error.message)
            await reply(interaction, reply_for(error, noun="team"))
        else:
            await reply(
                interaction,
                f"Reviews asked of the {linked} team will now ping <@&{role.id}>."
                f"{_a_ping_nobody_will_get(interaction, role)}",
            )

    return link_team


def _a_ping_nobody_will_get(interaction: discord.Interaction, role: discord.Role) -> str:
    """Warn when the mention this command promises will reach nobody, or say nothing.

    Discord notifies a role's members only if the role is mentionable or the sender holds Mention
    Everyone. Roles are created not mentionable, and neither of those is in the permission list
    the README gives, so on an ordinary server the ping renders as a blue pill in the thread and
    tells nobody. That is the one moment the whole team feature exists for, and it looks like it
    worked, so nobody finds out for days.

    A warning rather than a refusal: the link is still worth having, whoever runs this can fix it
    in one checkbox afterwards, and refusing over a permission would leave the team unlinked as
    well as unpinged. The ping itself is claimed before it is sent and stamped as spent whether
    or not anybody read it, so every review request that passes before the fix is silent.
    """
    if role.mentionable or interaction.app_permissions.mention_everyone:
        return ""
    return (
        f" Nobody will be notified by that yet: <@&{role.id}> is not mentionable, so Discord "
        "shows the mention without telling anyone. Turn on Allow Anyone To @mention This Role "
        "in the role's settings, or give this bot Mention @everyone, @here, and All Roles."
    )
