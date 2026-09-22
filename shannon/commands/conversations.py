"""`/log_conversation` and `/stop_conversation`: publishing a thread to its GitHub item.

A visible line goes into the thread first: the reply below is ephemeral and the rest of the
thread is about to be published. Capture needs a Developer Portal toggle nothing here can check.
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

    # discord.py's decorator leaves the binding parameter unsolved for a module-level command.
    return log_conversation  # pyright: ignore[reportUnknownVariableType]


def build_stop_conversation_command(
    service: LogsConversations, gate: PermissionGate
) -> SlashCommand:
    """Deliberately not given the capture flag.

    Turning capture off in a deployment that had it on leaves conversations open in the
    database, with the thread told logging is on and no way to stop it. So this one always works.
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
    item = f"{full_name}#{number}"
    if starting:
        return f"Logging this thread to {item}. Everyone in the thread has been told."
    return f"Stopped logging this thread to {item}. Anything still waiting will be published."
