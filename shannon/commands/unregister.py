"""`/unregister`: undo the binding, once GitHub has vouched for whoever is asking.

Run twice on purpose. A Discord interaction cannot wait on somebody opening a browser, so the
first run hands out a one-time link and the second one finishes the job. The same shape as the
duplicate-registration reply, which already tells people to run `/register` again to see where.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.unregistration import UnregisterOutcome

logger = logging.getLogger(__name__)


class VerifiesIdentity(Protocol):
    """Proving which GitHub account a Discord account belongs to."""

    @property
    def configured(self) -> bool: ...

    async def already_proved(self, *, guild_id: int, discord_user_id: int) -> str | None: ...

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str: ...


class UnregistersRepositories(Protocol):
    """Unbinding a repository from this server."""

    async def unregister(
        self, *, guild_id: int, full_name: str, login: str
    ) -> UnregisterOutcome: ...


def build_unregister_command(
    service: UnregistersRepositories, verification: VerifiesIdentity, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(name="unregister", description="Unbind this server's GitHub repository")
    # The full name is required and is a confirmation rather than a lookup: the server has exactly
    # one repository, so there is nothing to disambiguate. It is here because this is irreversible
    # and it cascades, and making somebody type the name is the cheapest guard available against
    # the command being run by accident.
    @app_commands.describe(
        repository="The repository's full name, owner/name, to confirm you mean it"
    )
    @app_commands.guild_only()
    async def unregister(interaction: discord.Interaction, repository: str) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("unregister", REGISTER_ROLES))
            return
        if not verification.configured:
            # Said rather than handing out a link that goes nowhere. The role check is above this
            # so that somebody who could not run the command anyway is not told how it is set up.
            await reply(
                interaction,
                "This bot cannot verify who you are on GitHub, so it will not unregister "
                "anything. An admin needs to set the GitHub App's client secret and this "
                "deployment's public URL.",
            )
            return

        await defer(interaction)
        try:
            login = await verification.already_proved(
                guild_id=interaction.guild_id, discord_user_id=interaction.user.id
            )
            if login is None:
                link = await verification.link_for(
                    guild_id=interaction.guild_id, discord_user_id=interaction.user.id
                )
                await reply(
                    interaction,
                    "First, prove to GitHub that you can administer this repository. Open this "
                    f"link, then run /unregister again:\n{link}",
                )
                return

            outcome = await service.unregister(
                guild_id=interaction.guild_id, full_name=repository, login=login
            )
        except ShannonError as error:
            logger.warning("unregister failed: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(interaction, _said(outcome))

    return unregister


def _said(outcome: UnregisterOutcome) -> str:
    """What was unbound, and what it cost.

    The orphaned threads are named because they are the surprising part. Nothing deletes them, so
    they stay in the channel saying things about a repository this bot no longer follows, and
    registering again opens a second thread for every one of those items rather than reusing them.
    """
    said = f"{outcome.full_name} is no longer mirrored in this server."
    if outcome.threads_orphaned:
        threads = "thread" if outcome.threads_orphaned == 1 else "threads"
        said += (
            f" The {outcome.threads_orphaned} {threads} already open stay where they are and stop "
            "updating; registering again opens new ones rather than reusing them."
        )
    return said
