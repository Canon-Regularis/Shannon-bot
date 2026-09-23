from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import words_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, owed, refused, reply
from shannon.discord_bot.slash import SlashCommand
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

# What to call each kind in a sentence, taken off the choices so there is one list of names and
# the one somebody picked from is the one they are read back.
_NAMES = {ObjectType(choice.value): choice.name for choice in CHOICES}


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
) -> SlashCommand:
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
        guild_id = await in_a_server(interaction, "set_channel", gate, REGISTER_ROLES)
        if guild_id is None:
            return
        refusal = why_threads_will_not_open(channel)
        if refusal is not None:
            await reply(
                interaction, refused(f"Threads cannot be opened in <#{channel.id}>. {refusal}")
            )
            return

        await defer(interaction)
        try:
            assignment = await service.assign(
                guild_id=guild_id,
                object_type=ObjectType(object_type.value),
                channel_id=channel.id,
            )
        except NotRegisteredError as error:
            # Through the reply table like every other error rather than around it, which is
            # what reading `error.message` here had been doing: the same refusal arrived red
            # from /refresh and uncoloured from here.
            await reply(interaction, refused(words_for(error, noun="repository")))
            return

        head = (
            f"{object_type.name.capitalize()} for {assignment.repository_name} will now "
            f"appear in <#{channel.id}>.{_stayed(assignment)}"
        )

        # The mapping is written by this point, so a failed relocation still has to report the
        # half that worked; saying nothing is how somebody runs it again and changes nothing.
        try:
            outcome = await relocation.relocate(
                guild_id=guild_id,
                object_type=ObjectType(object_type.value),
                channel_id=channel.id,
            )
        except ShannonError as error:
            logger.warning("/set_channel could not move the threads: %s", error.message)
            await reply(
                interaction,
                owed(
                    f"{head} The threads already open were left where they are. "
                    f"{words_for(error, noun='repository')}"
                ),
            )
        else:
            await reply(interaction, done(f"{head}{_said(outcome)}"))

    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` says why `Any` is the only truthful thing to put in.
    return set_channel  # pyright: ignore[reportUnknownVariableType]


def _stayed(assignment: ChannelAssignment) -> str:
    """Which kinds were left where they were, said before anything about what moved.

    A kind with no channel of its own went to this one, so setting this one used to take its
    threads along; it now gets a channel of its own instead, at the one it was already using
    (#134). That is a durable change the admin did not ask for, and an unsaid one would surface
    later as this bot ignoring a mapping they never knew they had.

    Not "the threads already open stay", which would be half the truth: new ones stay too, which
    is the point, so the sentence names the command that moves them rather than implying time
    will.
    """
    return "".join(
        f" {_NAMES[kept.object_type].capitalize()} stay in <#{kept.discord_channel_id}> "
        f"until /set_channel {_NAMES[kept.object_type]} moves them."
        for kept in assignment.pinned
    )


def _said(outcome: RelocationOutcome) -> str:
    """What became of the threads that were already open.

    Discord cannot move a thread, so "moved" means a replacement was opened in the new channel
    with the old one linked to it and locked.
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
