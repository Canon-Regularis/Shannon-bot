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
