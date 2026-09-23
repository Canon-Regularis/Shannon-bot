from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.layout import as_message
from shannon.discord_bot.panels import Accent, Block, BlockKind, Panel
from shannon.discord_bot.safe_text import MESSAGE_LIMIT

# Command replies stay ephemeral: thread traffic is the signal, an acknowledgement is not.
EPHEMERAL = True

# How an answer reads before it is read. Issue #147.
#
# Three outcomes and no more, which is the whole vocabulary a command reply has: it is finished,
# something is still owed, or it did not happen. The three constructors below are the only way to
# build a reply, so a command cannot word its own tone and the marks cannot drift apart.
#
# The mark is kept beside the accent rather than replaced by it, for the reason
# `formatting._PRIORITY_MARKS` already gives about lines read at a glance — and for one more that
# is specific to these. A refusal from a guard or a permission tier used to arrive as bare text
# with no bar at all, so the sentence was the only signal it carried; half the refusals in this
# project were uncoloured and nothing said so.
SUCCEEDED = "✅"
OWED = "⚠️"
REFUSED = "❌"


async def reply(interaction: discord.Interaction, message: Panel) -> None:
    """Answer an interaction whether or not it was already deferred.

    A `Panel` rather than a string, so a reply carrying no mark is not a thing this project can
    express: `done`, `owed` and `refused` are the only ways to build one and both type checkers
    hold it, which is a stronger promise than a test could make. A panel with no accent still
    goes out as ordinary text — `/mentions` reporting what it reads is the one that does.
    """
    content, view = as_message(message)
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

    A string and no mark, unlike `reply`. The marks say what became of a command somebody ran,
    and the person reading this did not run one.

    No `is_done` branch, unlike `reply`: its one caller neither defers nor answers first, because
    a public message can only ever be an interaction's first response.
    """
    await interaction.response.send_message(_fitted(message), ephemeral=False)


def done(message: str) -> Panel:
    """A command that worked and left nothing owing, in green."""
    return _marked(SUCCEEDED, message, Accent.OPEN)


def owed(message: str) -> Panel:
    """It happened, and something is still owed — by time, or by whoever ran it, in amber.

    Three things answer this way: a refusal that comes right on its own, a one-time link nobody
    has followed yet, and a command whose second half did not land after its first half did. None
    of them is a failure and none of them is finished, and reading either of those into it costs
    somebody the step they still have to take.
    """
    return _marked(OWED, message, Accent.MEDIUM)


def refused(message: str) -> Panel:
    """It did not happen and somebody has to put something right first, in red."""
    return _marked(REFUSED, message, Accent.FAILED)


def _marked(mark: str, message: str, accent: Accent) -> Panel:
    """One heading block, marked and coloured.

    Trimmed here rather than at each call site, and after the mark is on: several replies quote
    what the person typed back at them, a slash command argument can be far longer than a message
    may be, and an over-long one would make the refusal itself fail.
    """
    return Panel(blocks=(Block(BlockKind.HEADING, _fitted(f"{mark} {message}")),), accent=accent)


def _fitted(message: str) -> str:
    return message[: MESSAGE_LIMIT - 1] + "…" if len(message) > MESSAGE_LIMIT else message


async def _as_text(interaction: discord.Interaction, content: str) -> None:
    # Cut again, because a panel with no accent reaches here without having been: `Panel.trimmed`
    # measures against the four thousand a view may carry and this goes out as content, which may
    # carry two. Idempotent for everything the constructors above built.
    content = _fitted(content)
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
