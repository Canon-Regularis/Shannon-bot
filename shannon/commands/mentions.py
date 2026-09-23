from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.responses import defer, done, refused, reply
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

# Said wherever mentions are off: `<@&id>` reaches everybody holding the role, and Discord
# offers no way to leave one person out of one.
_ROLES_STILL_REACH_YOU = (
    " One thing this cannot turn off: a review asked of a GitHub team is a ping of the whole "
    "Discord role, and Discord gives nobody a way to leave one person out of one."
)


def build_mentions_command(service: RemembersWhoWantsPinging) -> SlashCommand:
    """The one command here that takes no permission gate, and the only one that should.

    It decides whether your own name notifies you, which is nobody else's business.
    `_permissions.UNGATED` names it and a test holds that list against the factories in this
    package, so a gate dropped from another command by accident goes red. No try/except either:
    this service raises no `ShannonError` to catch, the tree's own handler already answers an
    unexpected error, and a clause only a fake could reach is a branch nothing exercises under a
    coverage floor of a hundred per cent.
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
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        await defer(interaction)
        if state is None:
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

        said = _ON if wanted else _OFF + _ROLES_STILL_REACH_YOU
        # Reading the setting carries no mark. The three of them say what became of a change
        # and this made none; a tick on an answer to a question would be claiming one.
        await reply(interaction, Panel.of_text(said) if state is None else done(said))

    # `app_commands.command()` leaves the command's binding type unknown; one line here rather
    # than a suppression over the whole file.
    return mentions  # pyright: ignore[reportUnknownVariableType]
