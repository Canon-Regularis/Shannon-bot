"""The two commands that move an item through the workflow.

Both act on the thread they are run in, and take the state to move it to as a choice. It was
eight commands with no argument, one per state, on the reasoning that Discord shows them in
the picker as eight things to do. It also shows them as eight things to scroll past, and one
of the eight is `/set_med_priority`, which is not what anybody types first.

Two rather than one, because a priority is not a status. The service has a method for each,
the sentences differ, and the rules about a closed item and about DONE needing
READY_FOR_MERGE apply to status alone. One picker holding both would put High in a list
labelled status and need an isinstance to tell them apart again on the other side.

The choices are static, which is not a limitation being worked around. A choice list is baked
into the registration at `tree.sync()`, which runs once at boot, globally - so anything
per-guild or per-board would have to be an autocomplete instead. These values are a StrEnum:
the same five and the same three in every server, for the life of the process. Registered,
Discord renders a validated dropdown and discord.py resolves what comes back against the
list, so the callback has no parse to guard.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import DecidesGitHubAccess, github_allows, in_a_thread
from shannon.commands._permissions import WORKFLOW_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.errors import ShannonError
from shannon.github import people
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Written out rather than comprehended over the enum, and each table for its own reason.
#
# Status, because Discord shows choices in the order they are written and the enum is not in that
# order: it declares BACKLOG fourth, which is right for the database and wrong for a person, who
# would open the picker on "Not reviewed" with "Backlog" buried below "Ready for merge".
STATUS_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name=spoken(Status.BACKLOG), value=Status.BACKLOG.value),
    app_commands.Choice(name=spoken(Status.NOT_REVIEWED), value=Status.NOT_REVIEWED.value),
    app_commands.Choice(name=spoken(Status.IN_REVIEW), value=Status.IN_REVIEW.value),
    app_commands.Choice(name=spoken(Status.READY_FOR_MERGE), value=Status.READY_FOR_MERGE.value),
    app_commands.Choice(name=spoken(Status.DONE), value=Status.DONE.value),
]

# Priority, because `Priority` has a fourth member that must never reach the picker. UNSET is what
# an item has before anybody says, not something to pick: `priority_change` ends in a lookup
# against a table of three, so a picked "None" would be a KeyError reaching whoever ran it as
# "Something went wrong here". Eight commands could not express it and so were immune to it; a
# list comprehended over the enum would say it on the first try. A test holds this pair together.
PRIORITY_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name=spoken(Priority.HIGH), value=Priority.HIGH.value),
    app_commands.Choice(name=spoken(Priority.MEDIUM), value=Priority.MEDIUM.value),
    app_commands.Choice(name=spoken(Priority.LOW), value=Priority.LOW.value),
]


class MovesItems(Protocol):
    """Setting the status or the priority of the item a thread belongs to."""

    async def set_status(self, *, thread_id: int, status: Status) -> WorkflowOutcome: ...

    async def set_priority(self, *, thread_id: int, priority: Priority) -> WorkflowOutcome: ...


def build_workflow_commands(
    service: MovesItems, gate: PermissionGate, access: DecidesGitHubAccess
) -> tuple[SlashCommand, ...]:
    """The status command and the priority command."""
    return (_status_command(service, gate, access), _priority_command(service, gate, access))


def _status_command(
    service: MovesItems, gate: PermissionGate, access: DecidesGitHubAccess
) -> SlashCommand:
    @app_commands.command(name="status", description="Move this item to a workflow status")
    @app_commands.describe(to="The status to move it to")
    @app_commands.choices(to=STATUS_CHOICES)
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction, to: app_commands.Choice[str]) -> None:
        # No guard around this. discord.py resolves the incoming value against the list
        # registered above before the callback runs, so a value outside it cannot arrive, and
        # an arm nothing reaches is a hole under a total branch floor rather than caution.
        wanted = Status(to.value)
        await _act(
            interaction,
            "status",
            gate,
            access,
            lambda thread_id: service.set_status(thread_id=thread_id, status=wanted),
            said=spoken(wanted),
        )

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put in.
    return run  # pyright: ignore[reportUnknownVariableType]


def _priority_command(
    service: MovesItems, gate: PermissionGate, access: DecidesGitHubAccess
) -> SlashCommand:
    @app_commands.command(name="priority", description="Set this item's priority")
    @app_commands.describe(to="The priority to give it")
    @app_commands.choices(to=PRIORITY_CHOICES)
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction, to: app_commands.Choice[str]) -> None:
        wanted = Priority(to.value)
        await _act(
            interaction,
            "priority",
            gate,
            access,
            lambda thread_id: service.set_priority(thread_id=thread_id, priority=wanted),
            said=f"{spoken(wanted)} priority",
        )

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # explains why `Any` is the only truthful thing to put in.
    return run  # pyright: ignore[reportUnknownVariableType]


async def _act(
    interaction: discord.Interaction,
    name: str,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    call: Callable[[int], Awaitable[WorkflowOutcome]],
    *,
    said: str,
) -> None:
    """The half both of them share: check, defer, call, answer.

    Two checks, in this order on purpose. The Discord role first, because somebody
    without it should be told that rather than told about a GitHub account they may
    never have connected. Then GitHub, and only after the defer: this one makes a
    network call, and Discord allows three seconds for a first response.
    """
    where = await in_a_thread(interaction, name, gate, WORKFLOW_ROLES)
    if where is None:
        return

    await defer(interaction)
    if not await github_allows(interaction, access, where.guild_id, at_least=people.WRITE):
        return
    try:
        outcome = await call(where.channel_id)
    except ShannonError as error:
        # The value as well as the name. With eight commands the name said what was asked
        # for; with two it does not, and "/status could not finish" names neither the item
        # nor the state somebody wanted.
        logger.warning("/%s %s could not finish: %s", name, said, error.message)
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
