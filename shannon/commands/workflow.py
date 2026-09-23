"""The eight commands that move an item through the workflow.

None of them takes an argument: they act on the thread they are run in. Eight separate commands
rather than one with a choice, because Discord shows them in the picker as eight things to do.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_thread
from shannon.commands._permissions import WORKFLOW_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.errors import ShannonError
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Command name to the status it sets, written out rather than derived: people type these.
STATUS_COMMANDS: dict[str, Status] = {
    "set_backlog": Status.BACKLOG,
    "set_not_reviewed": Status.NOT_REVIEWED,
    "set_in_review": Status.IN_REVIEW,
    "set_ready_for_merge": Status.READY_FOR_MERGE,
    "set_done": Status.DONE,
}

PRIORITY_COMMANDS: dict[str, Priority] = {
    "set_high_priority": Priority.HIGH,
    "set_med_priority": Priority.MEDIUM,
    "set_low_priority": Priority.LOW,
}


class MovesItems(Protocol):
    """Setting the status or the priority of the item a thread belongs to."""

    async def set_status(self, *, thread_id: int, status: Status) -> WorkflowOutcome: ...

    async def set_priority(self, *, thread_id: int, priority: Priority) -> WorkflowOutcome: ...


def build_workflow_commands(service: MovesItems, gate: PermissionGate) -> tuple[SlashCommand, ...]:
    """Every status and priority command, built from the two tables above."""
    return tuple(
        [_status_command(name, status, service, gate) for name, status in STATUS_COMMANDS.items()]
        + [
            _priority_command(name, priority, service, gate)
            for name, priority in PRIORITY_COMMANDS.items()
        ]
    )


def _status_command(
    name: str, status: Status, service: MovesItems, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(name=name, description=f"Mark this item {spoken(status).lower()}")
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction) -> None:
        await _act(
            interaction,
            name,
            gate,
            lambda thread_id: service.set_status(thread_id=thread_id, status=status),
            said=spoken(status),
        )

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put in.
    return run  # pyright: ignore[reportUnknownVariableType]


def _priority_command(
    name: str, priority: Priority, service: MovesItems, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(
        name=name, description=f"Give this item {spoken(priority).lower()} priority"
    )
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction) -> None:
        await _act(
            interaction,
            name,
            gate,
            lambda thread_id: service.set_priority(thread_id=thread_id, priority=priority),
            said=f"{spoken(priority)} priority",
        )

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put in.
    return run  # pyright: ignore[reportUnknownVariableType]


async def _act(
    interaction: discord.Interaction,
    name: str,
    gate: PermissionGate,
    call: Callable[[int], Awaitable[WorkflowOutcome]],
    *,
    said: str,
) -> None:
    """The half every one of the eight shares: check, defer, call, answer."""
    where = await in_a_thread(interaction, name, gate, WORKFLOW_ROLES)
    if where is None:
        return

    await defer(interaction)
    try:
        outcome = await call(where.channel_id)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, done(_said(outcome, said)))


def _said(outcome: WorkflowOutcome, said: str) -> str:
    item = f"{outcome.full_name}#{outcome.number}"
    if outcome.lock_refused:
        # The move landed and the lock did not, so reporting only the refusal would read as
        # nothing having happened. Which way it was going matters: a thread that would not
        # unlock is one nobody can reply in.
        left = (
            "this thread could not be locked"
            if outcome.wanted_locked
            else "this thread could not be unlocked, so nobody can reply in it yet"
        )
        return (
            f"{item} is {said}, but {left}. Discord refused that, which is usually the bot "
            "missing Manage Threads. Run this again once it has it and the lock gets another go."
        )
    if not outcome.changed:
        # A repeat is not a failure: the requirements say a duplicate takes no action.
        return f"{item} is already {said}."
    if outcome.locked:
        return f"{item} is now {said}, and this thread is locked."
    return f"{item} is now {said}."
