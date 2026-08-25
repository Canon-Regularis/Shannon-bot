"""The eight commands that move an item through the workflow.

None of them takes an argument, because none is needed: they act on the thread they are run in,
which is the item the person running one is already looking at. Asking for a link as well would
be asking somebody to name the thing on their screen.

Eight separate commands rather than one with a choice, because that is what the requirements
list and because Discord shows them in the picker as eight things a reviewer can do.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import WORKFLOW_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, reply
from shannon.domain.enums import Priority, Status
from shannon.domain.errors import ShannonError
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Command name to the status it sets. The requirements name these, and the names are what people
# type, so they are written out rather than derived from the enum.
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


def build_workflow_commands(
    service: MovesItems, gate: PermissionGate
) -> tuple[app_commands.Command, ...]:
    """Every status and priority command, built from the two tables above.

    One builder rather than eight, because the eight differ only in the label they set and the
    sentence they answer with. Anything that has to differ further belongs in the service.
    """
    return tuple(
        [_status_command(name, status, service, gate) for name, status in STATUS_COMMANDS.items()]
        + [
            _priority_command(name, priority, service, gate)
            for name, priority in PRIORITY_COMMANDS.items()
        ]
    )


def _status_command(
    name: str, status: Status, service: MovesItems, gate: PermissionGate
) -> app_commands.Command:
    @app_commands.command(name=name, description=f"Mark this item {_spoken(status.value)}")
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction) -> None:
        await _act(
            interaction,
            name,
            gate,
            lambda thread_id: service.set_status(thread_id=thread_id, status=status),
            said=status.value,
        )

    return run


def _priority_command(
    name: str, priority: Priority, service: MovesItems, gate: PermissionGate
) -> app_commands.Command:
    @app_commands.command(
        name=name, description=f"Give this item {priority.value.lower()} priority"
    )
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction) -> None:
        await _act(
            interaction,
            name,
            gate,
            lambda thread_id: service.set_priority(thread_id=thread_id, priority=priority),
            said=f"{priority.value} priority",
        )

    return run


async def _act(interaction, name: str, gate: PermissionGate, call, *, said: str) -> None:
    """The half every one of the eight shares: check, defer, call, answer."""
    if interaction.guild_id is None:
        await reply(interaction, "Run this inside a server channel.")
        return
    if not gate.allows(interaction.user, WORKFLOW_ROLES):
        await reply(interaction, gate.denial(name, WORKFLOW_ROLES))
        return
    if interaction.channel_id is None:
        await reply(interaction, "Run this inside the item's thread.")
        return

    await defer(interaction)
    try:
        outcome = await call(interaction.channel_id)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, _said(outcome, said))


def _said(outcome: WorkflowOutcome, said: str) -> str:
    item = f"{outcome.full_name}#{outcome.number}"
    if outcome.lock_refused:
        # The move landed and the lock did not, and those two are one Discord permission apart.
        # Reporting only the refusal reads as nothing having happened, which is the opposite of
        # what did: the labels are on GitHub, the status is stored, the thread says so.
        #
        # Which way it was going matters to whoever reads this. A thread that would not lock is
        # untidy; a thread that would not unlock is one nobody can reply in, which is the thing
        # they just reopened it to do.
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
        # A repeat is not a failure. The requirements say a duplicate takes no action, and the
        # person running it wants to know the item is where they were putting it.
        return f"{item} is already {said}."
    if outcome.locked:
        return f"{item} is now {said}, and this thread is locked."
    return f"{item} is now {said}."


def _spoken(status: str) -> str:
    return status.replace("_", " ").lower()
