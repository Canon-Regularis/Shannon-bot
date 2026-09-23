"""`/unregister`: undo the binding, once GitHub has vouched for whoever is asking.

Run twice on purpose: a Discord interaction cannot wait on somebody opening a browser, so the
first run hands out a one-time link and the second one finishes the job.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.db.stores.identities import ProvedAccount
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import VerificationPurpose
from shannon.domain.errors import ShannonError
from shannon.services.unregistration import UnregisterOutcome

logger = logging.getLogger(__name__)


class VerifiesIdentity(Protocol):
    """Proving which GitHub account a Discord account belongs to.

    `proved_just_now` rather than whether they have ever proved anything: this permits an
    irreversible command, so what matters is that the person is at the keyboard now, having just
    come back from the browser.
    """

    @property
    def configured(self) -> bool: ...

    async def proved_just_now(
        self, *, guild_id: int, discord_user_id: int
    ) -> ProvedAccount | None: ...

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str: ...


class UnregistersRepositories(Protocol):
    """Unbinding a repository from this server."""

    async def unregister(
        self, *, guild_id: int, full_name: str, login: str
    ) -> UnregisterOutcome: ...


def build_unregister_command(
    service: UnregistersRepositories, verification: VerifiesIdentity, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(name="unregister", description="Unbind this server's GitHub repository")
    # A confirmation rather than a lookup: the server has exactly one repository. This is
    # irreversible and it cascades, so typing the name is the cheapest guard against an accident.
    @app_commands.describe(
        repository="The repository's full name, owner/name, to confirm you mean it"
    )
    @app_commands.guild_only()
    async def unregister(interaction: discord.Interaction, repository: str) -> None:
        guild_id = await in_a_server(interaction, "unregister", gate, REGISTER_ROLES)
        if guild_id is None:
            return
        if not verification.configured:
            # The role check is above this, so somebody who could not run the command anyway is
            # not told how the deployment is configured.
            await reply(
                interaction,
                "This bot cannot verify who you are on GitHub, so it will not unregister "
                "anything. An admin needs to set the GitHub App's client secret and this "
                "deployment's public URL.",
            )
            return

        await defer(interaction)
        try:
            proved = await verification.proved_just_now(
                guild_id=guild_id, discord_user_id=interaction.user.id
            )
            if proved is None:
                link = await verification.link_for(
                    guild_id=guild_id,
                    discord_user_id=interaction.user.id,
                    purpose=VerificationPurpose.UNREGISTER,
                )
                await reply(
                    interaction,
                    "First, prove to GitHub that you can administer this repository. Open this "
                    f"link, then run /unregister again:\n{link}",
                )
                return

            outcome = await service.unregister(
                guild_id=guild_id, full_name=repository, login=proved.login
            )
        except ShannonError as error:
            logger.warning("unregister failed: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(interaction, done(_said(outcome)))

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put there.
    return unregister  # pyright: ignore[reportUnknownVariableType]


def _said(outcome: UnregisterOutcome) -> str:
    said = f"{outcome.full_name} is no longer mirrored in this server."
    if outcome.threads_orphaned:
        threads = "thread" if outcome.threads_orphaned == 1 else "threads"
        said += (
            f" The {outcome.threads_orphaned} {threads} already open stay where they are and stop "
            "updating; registering again opens new ones rather than reusing them."
        )
    return said
