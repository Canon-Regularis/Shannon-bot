from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.slash import SlashCommand


class RemembersWhoWantsPinging(Protocol):
    """One member's own answer to whether this bot may notify them here."""

    async def wants_mentions(self, *, guild_id: int, discord_user_id: int) -> bool: ...

    async def set_mentions(self, *, guild_id: int, discord_user_id: int, wanted: bool) -> None: ...


# Discord shows the description; the value is what reaches the callback.
_CHOICES = [
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
]

_ON = "Mentions are on. This bot will notify you about items you are on."

_OFF = (
    "Mentions are off. You will still be named on every item you are on and in every comment "
    "that tags you, as a mention this bot will not ring."
)

# Said wherever mentions are off, because it is the one thing this command cannot do and finding
# it out from a notification is worse than being told. `<@&id>` reaches everybody holding the
# role and Discord offers no way to leave one person out of one.
_ROLES_STILL_REACH_YOU = (
    " One thing this cannot turn off: a review asked of a GitHub team is a ping of the whole "
    "Discord role, and Discord gives nobody a way to leave one person out of one."
)


def build_mentions_command(service: RemembersWhoWantsPinging) -> SlashCommand:
    """The first command here that takes no permission gate, and the only one that should.

    Every other command in this bot decides something about the server: which repository it
    mirrors, where threads go, whose login is whose, what an item's status is. This one decides
    whether your own name notifies you, which is nobody else's business, and gating it would mean
    asking a project manager to turn off your own pings.

    `_permissions.UNGATED` names it, and a test holds that list against what the factories in this
    package actually take, so a second ungated command has to be added there on purpose and a gate
    dropped from another by accident goes red.

    No try/except either, which every other command here has. This service raises no `ShannonError`
    for one to catch, and a database that has gone away raises something `reply_for` would not
    match anyway; the tree's own handler already answers an unexpected error. A clause only a fake
    could reach would be a branch nothing exercises, under a coverage floor of a hundred percent.
    """

    @app_commands.command(
        name="mentions", description="Whether this bot's messages notify you in this server"
    )
    @app_commands.describe(state="on to be notified, off to be named without being notified")
    @app_commands.choices(state=_CHOICES)
    @app_commands.guild_only()
    async def mentions(
        interaction: discord.Interaction, state: app_commands.Choice[str] | None = None
    ) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return

        await defer(interaction)
        if state is None:
            # Asked rather than set. Somebody who cannot remember which way round they are should
            # be able to find out without changing it.
            wanted = await service.wants_mentions(
                guild_id=interaction.guild_id, discord_user_id=interaction.user.id
            )
        else:
            wanted = state.value == "on"
            await service.set_mentions(
                guild_id=interaction.guild_id,
                discord_user_id=interaction.user.id,
                wanted=wanted,
            )

        await reply(interaction, _ON if wanted else _OFF + _ROLES_STILL_REACH_YOU)

    return mentions
