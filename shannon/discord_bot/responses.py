from __future__ import annotations

import discord

# Command replies stay ephemeral. Thread traffic is the signal; an "ok, registered" seen by
# everyone is not.
EPHEMERAL = True


async def reply(interaction: discord.Interaction, message: str) -> None:
    """Answer an interaction whether or not it was already deferred."""
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
