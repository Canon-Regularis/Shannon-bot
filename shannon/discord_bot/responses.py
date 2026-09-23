from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.layout import as_message
from shannon.discord_bot.panels import Accent, Block, BlockKind, Panel
from shannon.discord_bot.safe_text import MESSAGE_LIMIT

# Command replies stay ephemeral: thread traffic is the signal, an acknowledgement is not.
EPHEMERAL = True


async def reply(interaction: discord.Interaction, message: str | Panel) -> None:
    """Answer an interaction whether or not it was already deferred.

    Trimmed to Discord's limit here rather than at each call site: several replies quote what
    the person typed back at them, and a slash command argument can be far longer than a message
    may be, so an over-long one would make the refusal itself fail.
    """
    if isinstance(message, Panel):
        content, view = as_message(message)
    else:
        content, view = _fitted(message), None

    if view is not None:
        await _as_components(interaction, view)
    else:
        await _as_text(interaction, content or "")


async def in_the_channel(interaction: discord.Interaction, message: str) -> None:
    """The one message this bot sends that everybody in the channel can see.

    `EPHEMERAL` above is the rule and this is its single exception, which is argued rather than
    excused. An acknowledgement is not signal, and that is why every other reply is private. This
    one is not an acknowledgement: it is addressed to somebody who did not run the command and is
    not watching for a reply, and an ephemeral message reaches exactly one person — the one who
    already knows. A private reply here would be the bot telling somebody it had asked a question
    it had not asked.

    `ephemeral=False` is passed rather than left out, although it is discord.py's own default, so
    that the exception is visible where it happens and not only where it is defined.

    No `is_done` branch, unlike `reply`: its one caller neither defers nor answers first, because
    a public message can only ever be an interaction's first response.
    """
    await interaction.response.send_message(_fitted(message), ephemeral=False)


def done(message: str) -> Panel:
    """A command that worked, in green."""
    return Panel(blocks=(Block(BlockKind.HEADING, _fitted(message)),), accent=Accent.OPEN)


def _fitted(message: str) -> str:
    return message[: MESSAGE_LIMIT - 1] + "…" if len(message) > MESSAGE_LIMIT else message


async def _as_text(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=EPHEMERAL)
    else:
        await interaction.response.send_message(content, ephemeral=EPHEMERAL)


async def _as_components(interaction: discord.Interaction, view: ui.LayoutView) -> None:
    """The reply as a card, which carries no content at all.

    Its own call rather than a keyword on the one above: discord.py types content and components
    as separate overloads, and the absence of `content` is what selects this one, so passing
    `None` is not the same thing.
    """
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=EPHEMERAL)
    else:
        await interaction.response.send_message(view=view, ephemeral=EPHEMERAL)


async def defer(interaction: discord.Interaction) -> None:
    """Buy time before work that talks to GitHub or the database.

    Discord drops an interaction that goes unanswered for three seconds.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=EPHEMERAL)
