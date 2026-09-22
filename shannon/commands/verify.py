"""`/verify`: prove to GitHub who you are, and have the bot write the link itself.

Run twice, for the reason `/unregister` is: a Discord interaction cannot wait on somebody opening
a browser, so the first run hands out a one-time link and the second one finishes the job.

Ungated, and that is the point rather than an oversight. `/link` is gated because it is a claim
about somebody's account that nobody checks, so an ungated one lets anybody take any login. There
is nothing to gate here: GitHub says who followed the link, and the only account anybody can bind
this way is the one they just signed into. It decides something about your own name, like
`/mentions`, and unlike every other command it cannot be wrong.

Nobody types a login either, which closes the other half. The login is whatever `GET /user`
answered for the account that authorised, so a typo binds nothing and a stranger's name cannot be
bound by mistake.
"""

from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.db.stores.identities import ProvedAccount
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand

NOT_CONFIGURED = (
    "This bot cannot check who you are on GitHub, so there is nothing to prove to. An admin "
    "needs to set the GitHub App's client secret and this deployment's public URL."
)


class ProvesIdentity(Protocol):
    """Proving which GitHub account a Discord account belongs to.

    The same three questions `/unregister` asks, which is why the shape matches: whether this
    deployment can do it at all, whether they have just done it, and the link that lets them.
    """

    @property
    def configured(self) -> bool: ...

    async def proved_just_now(
        self, *, guild_id: int, discord_user_id: int
    ) -> ProvedAccount | None: ...

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str: ...


class BindsProvedAccounts(Protocol):
    """Recording a GitHub account against a Discord one, where GitHub has vouched for it."""

    async def bind(
        self, *, guild_id: int, discord_user_id: int, login: str, github_user_id: int
    ) -> str: ...


def build_verify_command(
    service: BindsProvedAccounts, verification: ProvesIdentity
) -> SlashCommand:
    """No gate, and no try/except either.

    The gate is absent because there is nothing here anybody could misuse: the only account this
    can bind is the one whoever ran it has just signed into. `_permissions.UNGATED` names it and a
    test holds that set against the factories in this package.

    The try/except is absent because nothing on this path raises a `ShannonError`. `link` refuses
    a login GitHub has never heard of; this one has no login to doubt, so a clause catching one
    would be a branch nothing can exercise under a coverage floor of a hundred per cent.
    """

    @app_commands.command(
        name="verify", description="Prove your GitHub account and link it to yourself"
    )
    @app_commands.guild_only()
    async def verify(interaction: discord.Interaction) -> None:
        # The guild check by hand rather than through `in_a_server`, which exists to apply a tier
        # and there is no tier here. `/mentions` does the same, for the same reason.
        guild_id = interaction.guild_id
        if guild_id is None:
            await reply(interaction, NOT_IN_A_SERVER)
            return
        if not verification.configured:
            await reply(interaction, NOT_CONFIGURED)
            return

        await defer(interaction)
        proved = await verification.proved_just_now(
            guild_id=guild_id, discord_user_id=interaction.user.id
        )
        if proved is None:
            link = await verification.link_for(
                guild_id=guild_id, discord_user_id=interaction.user.id
            )
            await reply(
                interaction,
                "Open this link to sign in to GitHub, then run /verify again:\n" + link,
            )
            return

        login = await service.bind(
            guild_id=guild_id,
            discord_user_id=interaction.user.id,
            login=proved.login,
            github_user_id=proved.github_user_id,
        )
        await reply(
            interaction,
            done(
                f"GitHub says you are {login}, and this server now has that on record. "
                "Anything this bot does on your behalf will go to that account."
            ),
        )

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put there.
    return verify  # pyright: ignore[reportUnknownVariableType]
