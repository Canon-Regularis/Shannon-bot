from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.threads import why_threads_will_not_open
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError, ShannonError
from shannon.services.channels import ChannelAssignment
from shannon.services.sync.relocation import RelocationOutcome

logger = logging.getLogger(__name__)

# All three kinds this bot mirrors. Tickets have no fallback channel, unlike issues, so a board
# that nobody has mapped a channel for stays unmirrored until somebody runs this.
CHOICES = [
    app_commands.Choice(name="pull requests", value=ObjectType.PR.value),
    app_commands.Choice(name="issues", value=ObjectType.ISSUE.value),
    app_commands.Choice(name="project tickets", value=ObjectType.TICKET.value),
]


class RelocatesThreads(Protocol):
    """Giving the threads a changed mapping left behind ones in the channel it now names."""

    async def relocate(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> RelocationOutcome: ...


class MapsChannels(Protocol):
    """Pointing one kind of item at the channel its threads appear in."""

    async def assign(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> ChannelAssignment: ...


def build_set_channel_command(
    service: MapsChannels, relocation: RelocatesThreads, gate: PermissionGate
) -> app_commands.Command:
    @app_commands.command(
        name="set_channel", description="Choose which channel a kind of GitHub item posts into"
    )
    @app_commands.describe(
        object_type="Which kind of GitHub item", channel="Where its threads should appear"
    )
    @app_commands.choices(object_type=CHOICES)
    @app_commands.guild_only()
    async def set_channel(
        interaction: discord.Interaction,
        object_type: app_commands.Choice[str],
        channel: discord.TextChannel | discord.ForumChannel,
    ) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, REGISTER_ROLES):
            await reply(interaction, gate.denial("set_channel", REGISTER_ROLES))
            return
        refusal = why_threads_will_not_open(channel)
        if refusal is not None:
            await reply(interaction, f"<#{channel.id}> cannot hold threads. {refusal}")
            return

        await defer(interaction)
        try:
            assignment = await service.assign(
                guild_id=interaction.guild_id,
                object_type=ObjectType(object_type.value),
                channel_id=channel.id,
            )
        except NotRegisteredError as error:
            await reply(interaction, error.message)
            return

        head = (
            f"{object_type.name.capitalize()} for {assignment.repository_name} will now "
            f"appear in <#{channel.id}>."
        )

        # The mapping is written by this point and has to be reported as written whatever happens
        # next. A relocation that fails leaves the command half done, and saying nothing about the
        # half that worked is how somebody runs it again and changes nothing.
        try:
            outcome = await relocation.relocate(
                guild_id=interaction.guild_id,
                object_type=ObjectType(object_type.value),
                channel_id=channel.id,
            )
        except ShannonError as error:
            logger.warning("/set_channel could not move the threads: %s", error.message)
            await reply(
                interaction,
                f"{head} The threads already open were left where they are. "
                f"{reply_for(error, noun='repository')}",
            )
        else:
            await reply(interaction, f"{head}{_said(outcome)}")

    return set_channel


def _said(outcome: RelocationOutcome) -> str:
    """What became of the threads that were already open.

    This used to say they stayed where they were, which was true and was the whole bug: Discord
    cannot move a thread, so the honest answer was "nothing happens to them", and an admin who had
    registered into the wrong channel had no way to put it right. The word "moved" was refused
    here once, on the grounds that it would send somebody looking for threads that never went
    anywhere. It is accurate now, and the clause after it says exactly what it means.
    """
    if outcome.moved == 0 and outcome.left == 0:
        return " No threads were left in another channel."

    threads = "thread" if outcome.moved == 1 else "threads"
    said = (
        f" {outcome.moved} {threads} already open moved there too; each old one now links to its "
        "replacement and is locked."
    )
    if outcome.left:
        is_are = "is" if outcome.left == 1 else "are"
        said += (
            f" {outcome.left} {is_are} still in another channel, so run /set_channel again to "
            "carry on."
        )
    if outcome.failed:
        is_are = "is" if outcome.failed == 1 else "are"
        said += (
            f" {outcome.failed} could not be moved just now and {is_are} among those; "
            "the log says why."
        )
    return said
