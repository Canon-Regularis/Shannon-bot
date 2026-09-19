from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.layout import as_message
from shannon.discord_bot.panels import Accent, Block, BlockKind, Panel
from shannon.discord_bot.safe_text import MESSAGE_LIMIT

# Command replies stay ephemeral. Thread traffic is the signal; an "ok, registered" seen by
# everyone is not.
EPHEMERAL = True


async def reply(interaction: discord.Interaction, message: str | Panel) -> None:
    """Answer an interaction whether or not it was already deferred.

    A string goes as a string, and most of them are. A bar drawn down the side of one sentence
    is louder than the sentence and says nothing the sentence does not; `done` and the refusal
    table below are for the answers where it says something real.

    Trimmed to Discord's limit here rather than at each call site. Several replies quote what
    the person typed back at them, and a slash command argument can be far longer than a
    message may be, so an over-long argument would otherwise make the refusal itself fail and
    leave them with nothing at all.
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
    """A command that worked, in green.

    For the answers with more than one part to them: what was done, and what that means for the
    threads or the people it was done to. The colour is what a reader takes in first, and on
    those it is worth having, because the sentence after it takes a moment to read.
    """
    return Panel(blocks=(Block(BlockKind.HEADING, _fitted(message)),), accent=Accent.OPEN)


def _fitted(message: str) -> str:
    return message[: MESSAGE_LIMIT - 1] + "…" if len(message) > MESSAGE_LIMIT else message


async def _as_text(interaction: discord.Interaction, content: str) -> None:
    """The reply as a string, which is what most of them are."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=EPHEMERAL)
    else:
        await interaction.response.send_message(content, ephemeral=EPHEMERAL)


async def _as_components(interaction: discord.Interaction, view: ui.LayoutView) -> None:
    """The reply as a card, which carries no content at all.

    Its own call rather than a keyword on the one above, because Discord refuses a message
    carrying both and discord.py types the two as separate overloads: the absence of `content`
    is what selects the components one, so passing it as `None` is not the same thing.
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
