from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.db.stores.identities import ProvedAccount
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, owed, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.discord_bot.threads import why_threads_will_not_open
from shannon.domain.enums import VerificationPurpose
from shannon.domain.errors import ShannonError
from shannon.services.registration import RegistrationResult

logger = logging.getLogger(__name__)

NOT_CONFIGURED = (
    "This bot cannot check who you are on GitHub, so it will not bind a repository to this "
    "server. An admin needs to set the GitHub App's client secret and this deployment's public "
    "URL."
)


class RegistersRepositories(Protocol):
    """Binding a GitHub repository to this server."""

    async def register(
        self, *, guild_id: int, channel_id: int, link: str, login: str
    ) -> RegistrationResult: ...


class VerifiesIdentity(Protocol):
    """Proving which GitHub account somebody holds, right now.

    `proved_just_now` rather than ever: binding a repository discloses everything in it to a
    Discord channel, so what matters is that the person is at the keyboard having just come back
    from the browser, not that they once were.
    """

    @property
    def configured(self) -> bool: ...

    async def proved_just_now(
        self, *, guild_id: int, discord_user_id: int
    ) -> ProvedAccount | None: ...

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str: ...


def build_register_command(
    service: RegistersRepositories, verification: VerifiesIdentity, gate: PermissionGate
) -> SlashCommand:
    """Two runs, for the reason `/unregister` takes two. Issue #135.

    A Discord role said who could bind a repository, and a guild administrator holds that role
    automatically in every server this bot was invited to. So anybody who administered any server
    could mirror any repository the App is installed on — including a private one they had no
    GitHub relationship with — into a channel of their choosing.

    The role gate stays, and stays first: minting a link writes an unauthenticated row, and
    somebody who could not run the command anyway is not told how the deployment is configured.
    """

    @app_commands.command(
        name="register", description="Bind a GitHub repository to this Discord server"
    )
    @app_commands.describe(github_repo_link="Link to the GitHub repository")
    @app_commands.guild_only()
    async def register(interaction: discord.Interaction, github_repo_link: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, refused(gate.denial("register", REGISTER_ROLES)))
            return
        # The channel this was run in becomes the home for pull request threads. Refusing here
        # is the last chance to say so: the sync path hits it hours later with nobody to tell.
        refusal = why_threads_will_not_open(interaction.channel)
        if refusal is not None:
            await reply(interaction, refused(f"Threads cannot be opened here. {refusal}"))
            return
        # Above the browser trip rather than after it: sending somebody to GitHub and then
        # refusing them on something already known is a round trip spent for nothing.
        if not verification.configured:
            await reply(interaction, refused(NOT_CONFIGURED))
            return

        await defer(interaction)
        try:
            proved = await verification.proved_just_now(
                guild_id=interaction.guild_id, discord_user_id=interaction.user.id
            )
            if proved is None:
                link = await verification.link_for(
                    guild_id=interaction.guild_id,
                    discord_user_id=interaction.user.id,
                    purpose=VerificationPurpose.REGISTER,
                )
                await reply(
                    interaction,
                    owed(
                        "First, prove to GitHub that you administer this repository. Open this "
                        "link, then run /register again with the same repository "
                        f"link:\n{link}"
                    ),
                )
                return

            result = await service.register(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                link=github_repo_link,
                login=proved.login,
            )
        except ShannonError as error:
            logger.warning("register failed: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(
                interaction,
                done(
                    f"Registered {result.full_name}. Pull request threads will appear "
                    f"in <#{result.pr_channel_id}>."
                ),
            )

    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` says why `Any` is the only truthful thing to put in.
    return register  # pyright: ignore[reportUnknownVariableType]
