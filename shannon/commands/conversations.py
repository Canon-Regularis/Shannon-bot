"""`/log_conversation` and `/stop_conversation`: publishing a thread to its GitHub item.

Issue #103. Everything else in this project runs the other way, so these are the only commands
whose effect is to start reading Discord rather than to write to GitHub once.

Two things here are unlike the other command modules. The service posts a visible line into the
thread before anything is armed, because the ephemeral reply below is seen by one person and
everybody else in the thread is about to have their words published. And `/log_conversation`
refuses outright when the deployment has not turned capture on, since the privileged intent it
needs is a Developer Portal toggle that this process cannot check for itself.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_thread
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError

logger = logging.getLogger(__name__)

NOT_CAPTURING = (
    "This bot is not set up to read messages, so there is nothing to log. An admin needs to turn "
    "the message content intent on in the Discord Developer Portal and set "
    "`SHANNON_CAPTURE_DISCORD_MESSAGES=true`."
)


class LogsConversations(Protocol):
    """Starting and stopping the publishing of a thread, answering with the item it goes to."""

    async def start(self, *, thread_id: int, by: int) -> tuple[str, int]: ...

    async def stop(self, *, thread_id: int, by: int) -> tuple[str, int]: ...


def build_log_conversation_command(
    service: LogsConversations, gate: PermissionGate, *, capturing: bool
) -> SlashCommand:
    @app_commands.command(
        name="log_conversation", description="Publish this thread to the item's GitHub comments"
    )
    @app_commands.guild_only()
    async def log_conversation(interaction: discord.Interaction) -> None:
        await _act(interaction, "log_conversation", gate, service, capturing=capturing)

    # discord.py's decorator leaves the binding parameter unsolved for a module-level command, so
    # the object it hands back is `Command[Unknown, ...]` whatever this is declared as.
    return log_conversation  # pyright: ignore[reportUnknownVariableType]


def build_stop_conversation_command(
    service: LogsConversations, gate: PermissionGate
) -> SlashCommand:
    """Deliberately not given the capture flag.

    Turning capture off in a deployment that had it on leaves conversations open in the database.
    Nothing is captured into them, but somebody looking at a thread has been told logging is on and
    has no way to make that stop. So this one always works.
    """

    @app_commands.command(
        name="stop_conversation", description="Stop publishing this thread to GitHub"
    )
    @app_commands.guild_only()
    async def stop_conversation(interaction: discord.Interaction) -> None:
        await _act(interaction, "stop_conversation", gate, service, capturing=True)

    return stop_conversation  # pyright: ignore[reportUnknownVariableType]


async def _act(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    service: LogsConversations,
    *,
    capturing: bool,
) -> None:
    """The half both commands share: check, defer, call, answer."""
    where = await in_a_thread(interaction, command, gate, SYNC_ROLES)
    if where is None:
        return
    if not capturing:
        await reply(interaction, NOT_CAPTURING)
        return

    starting = command == "log_conversation"
    await defer(interaction)
    try:
        act = service.start if starting else service.stop
        full_name, number = await act(thread_id=where.channel_id, by=interaction.user.id)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", command, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, done(_said(full_name, number, starting=starting)))


def _said(full_name: str, number: int, *, starting: bool) -> str:
    """What happened, and for the start, that the thread was told.

    Worth saying back. The notice is the thing that makes publishing somebody's words defensible,
    and whoever ran the command cannot see their own ephemeral reply and the thread line together.
    """
    item = f"{full_name}#{number}"
    if starting:
        return f"Logging this thread to {item}. Everyone in the thread has been told."
    return f"Stopped logging this thread to {item}. Anything still waiting will be published."
