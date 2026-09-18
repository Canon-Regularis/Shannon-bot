"""`/assign` and `/unassign`: put somebody on this thread's item, or take them off.

Run inside the item's own thread, the way the `/set_*` commands are, and take one argument: who.
Inside a thread Discord's `channel_id` IS the thread id, so the item needs no naming.

What GitHub is asked for depends on what the thread is. A pull request gets a review request; an
issue gets an assignee, because an issue has no reviewers. That is GitHub's split rather than one
invented here, and somebody looking at the item should not have to know about it.

Neither command posts into the thread. GitHub sends the change back as a delivery, and the ordinary
mirror rewrites the block and says who was asked, exactly once.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.assignment import AssignmentOutcome

logger = logging.getLogger(__name__)


class PutsSomebodyOnAnItem(Protocol):
    """Changing who is on the item a thread belongs to."""

    async def assign(self, *, thread_id: int, discord_user_id: int) -> AssignmentOutcome: ...

    async def unassign(self, *, thread_id: int, discord_user_id: int) -> AssignmentOutcome: ...


def build_assign_command(service: PutsSomebodyOnAnItem, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="assign",
        description="Ask someone to review this pull request, or put them on this issue",
    )
    @app_commands.describe(member="Who to put on this item")
    @app_commands.guild_only()
    async def assign(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "assign", gate, member, service.assign)

    # discord.py's decorator leaves the binding parameter unsolved for a module-level
    # command, so the object it hands back is `Command[Unknown, ...]` whatever this is
    # declared as. `discord_bot/slash.py` argues why `Any` is the only truthful thing to put
    # in that slot; this silences pyright noticing the same gap a second time.
    return assign  # pyright: ignore[reportUnknownVariableType]


def build_unassign_command(service: PutsSomebodyOnAnItem, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="unassign", description="Take someone off this item's reviewers or assignees"
    )
    @app_commands.describe(member="Who to take off this item")
    @app_commands.guild_only()
    async def unassign(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "unassign", gate, member, service.unassign)

    # discord.py's decorator leaves the binding parameter unsolved for a module-level
    # command, so the object it hands back is `Command[Unknown, ...]` whatever this is
    # declared as. `discord_bot/slash.py` argues why `Any` is the only truthful thing to put
    # in that slot; this silences pyright noticing the same gap a second time.
    return unassign  # pyright: ignore[reportUnknownVariableType]


class _Change(Protocol):
    """One of the service's two methods, which take and answer the same things."""

    async def __call__(self, *, thread_id: int, discord_user_id: int) -> AssignmentOutcome: ...


async def _act(
    interaction: discord.Interaction,
    name: str,
    gate: PermissionGate,
    member: discord.Member,
    call: _Change,
) -> None:
    """The half both commands share: check, defer, call, answer."""
    if interaction.guild_id is None:
        await reply(interaction, "Run this inside a server channel.")
        return
    if not gate.allows(interaction.user, SYNC_ROLES):
        await reply(interaction, gate.denial(name, SYNC_ROLES))
        return
    if interaction.channel_id is None:
        await reply(interaction, "Run this inside the item's thread.")
        return

    await defer(interaction)
    try:
        outcome = await call(thread_id=interaction.channel_id, discord_user_id=member.id)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, _said(outcome, member.id))


def _said(outcome: AssignmentOutcome, discord_user_id: int) -> str:
    """What happened, named as the two different things they are.

    The person is written as the mention the command was given rather than as the GitHub login it
    resolved to. Both are true, and the one somebody picked out of a list is the one they will
    recognise in the answer.
    """
    item = f"{outcome.full_name}#{outcome.number}"
    who = f"<@{discord_user_id}>"
    if outcome.reviewing:
        if outcome.added:
            return f"Asked {who} for a review on {item}."
        return f"Withdrew the review request from {who} on {item}."
    if outcome.added:
        return f"Assigned {who} to {item}."
    return f"Took {who} off {item}."
