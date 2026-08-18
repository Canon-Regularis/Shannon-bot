from __future__ import annotations

import discord

from shannon.discord_bot.safe_text import MESSAGE_LIMIT

# Command replies stay ephemeral. Thread traffic is the signal; an "ok, registered" seen by
# everyone is not.
EPHEMERAL = True


async def reply(interaction: discord.Interaction, message: str) -> None:
    """Answer an interaction whether or not it was already deferred.

    Trimmed to Discord's limit here rather than at each call site. Several replies quote what
    the person typed back at them, and a slash command argument can be far longer than a
    message may be, so an over-long argument would otherwise make the refusal itself fail and
    leave them with nothing at all.
    """
    if len(message) > MESSAGE_LIMIT:
        message = message[: MESSAGE_LIMIT - 1] + "…"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=EPHEMERAL)
    else:
        await interaction.response.send_message(message, ephemeral=EPHEMERAL)


async def defer(interaction: discord.Interaction) -> None:
    """Buy time before work that talks to GitHub or the database.

    Discord drops an interaction that goes unanswered for three seconds.
    """
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=EPHEMERAL)
