"""The two commands that move an item through the workflow.

Both act on the thread they are run in, and take the state to move it to as a choice. It was
eight commands with no argument, one per state, on the reasoning that Discord shows them in
the picker as eight things to do. It also shows them as eight things to scroll past, and one
of the eight is `/set_med_priority`, which is not what anybody types first.

Two rather than one, because a priority is not a status. The service has a method for each,
the sentences differ, and the rules about a closed item and about the order a board's columns
come in apply to status alone. One picker holding both would put High in a list labelled
status and need an isinstance to tell them apart again on the other side.

Priority's choices are static and status's are not, and the difference is the board. A choice
list is baked into the registration at `tree.sync()`, which runs once at boot, globally, so
anything per-guild or per-board has to be an autocomplete. A priority is the same three
everywhere and stays a validated dropdown, where Discord resolves what comes back and the
callback has no parse to guard. A status is whatever columns somebody arranged on their board,
so it is a picker, and everything a picker costs falls due here: three seconds to answer,
nowhere to put a refusal, and a callback that must parse what arrives because a suggestion is
only a suggestion.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

import discord
from discord import app_commands

from shannon.commands._guards import DecidesGitHubAccess, github_allows, in_a_thread
from shannon.commands._permissions import WORKFLOW_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.board import status_from_column
from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.errors import ShannonError
from shannon.github import people
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Discord's own ceilings on a picker. It sends a longer list back as an error rather than
# truncating it, and rejects a choice whose value runs past a hundred characters - either way the
# whole list goes, so the picker fails rather than one entry.
MOST_CHOICES = 25
MOST_CHARACTERS = 100

Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]

# What the picker falls back to, and what it offers behind a board's own columns. Written out
# rather than comprehended over the enum, and each list for its own reason.
#
# Status, because a picker shows entries in the order it is given them and the enum is not in
# that order: it declares BACKLOG third, which is right for the database and wrong for a person,
# who would open the picker on "Not reviewed" with "Backlog" buried below "In review". The order
# written here is also the order a board's columns usually run in, which is no accident and no
# guarantee: the rule that refuses a skipped column reads the BOARD's order, never this list.
#
# Every one of these is a key in `_COLUMNS`, so `status_from_column` reads them all back. A test
# in `test_the_words_for_a_state.py` holds that, and without it this picker would offer names its
# own callback then refused.
OWN_NAMES: tuple[str, ...] = (
    spoken(Status.BACKLOG),
    spoken(Status.NOT_REVIEWED),
    spoken(Status.IN_REVIEW),
    spoken(Status.DONE),
)

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

    async def set_status(
        self,
        *,
        thread_id: int,
        status: Status,
        column: str = "",
        acting: int | None = None,
    ) -> WorkflowOutcome: ...

    async def set_priority(
        self, *, thread_id: int, priority: Priority, acting: int | None = None
    ) -> WorkflowOutcome: ...


class OffersColumns(Protocol):
    """Which columns this server's board has, for the picker to offer."""

    async def offered(self, guild_id: int) -> tuple[str, ...]: ...


def build_workflow_commands(
    service: MovesItems,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    columns: OffersColumns,
) -> tuple[SlashCommand, ...]:
    """The status command and the priority command."""
    return (
        _status_command(service, gate, access, columns),
        _priority_command(service, gate, access),
    )


def _status_command(
    service: MovesItems,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    columns: OffersColumns,
) -> SlashCommand:
    @app_commands.command(name="status", description="Move this item to a workflow status")
    @app_commands.describe(to="A column on this server's board, or one of this bot's own names")
    @app_commands.guild_only()
    async def run(interaction: discord.Interaction, to: str) -> None:
        # Parsed rather than trusted, which a closed choice list used to make unnecessary. An
        # autocomplete offers suggestions: what arrives may have been typed, may be a column
        # from the board this repository mirrored last week, or may be prose.
        wanted = status_from_column(to)
        if wanted is None:
            await reply(interaction, refused(_not_a_status(to)))
            return
        # The text goes down as the column too. The status decides the label, the lock and the
        # block in Discord; the column decides which of the board's own columns the card lands
        # in, and they are not the same question on a board with two columns for one status.
        await _act(
            interaction,
            "status",
            gate,
            access,
            lambda thread_id: service.set_status(
                thread_id=thread_id,
                status=wanted,
                column=to.strip(),
                # Whoever ran it, so the card is moved as them. Issue #170.
                acting=interaction.user.id,
            ),
            said=to.strip(),
        )

    run.autocomplete("to")(_suggesting(columns))

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
            lambda thread_id: service.set_priority(
                thread_id=thread_id, priority=wanted, acting=interaction.user.id
            ),
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
        return f"{item} is already {said}.{_no_column(outcome, said)}"
    if outcome.locked:
        return f"{item} is now {said}, and this thread is locked.{_no_column(outcome, said)}"
    return f"{item} is now {said}.{_no_column(outcome, said)}"


def _no_column(outcome: WorkflowOutcome, said: str) -> str:
    """Say the board had nowhere to put this, or say nothing.

    The only board refusal a person is told about, and it is told as a caveat on a success
    rather than as a failure, because that is what it is: the labels are on GitHub, the row
    is written and the thread is redrawn. The same shape the refused lock above takes, for
    the same reason - reporting only the half that did not land reads as nothing having
    happened.

    The other three ways a card can stay put say nothing. No project token and a board that
    cannot be read at all are invisible and identical for every command until an operator
    changes something; a warning attached to a fix the caller cannot make is noise. This one
    they will see for themselves the moment they open the board.

    It does not claim which column the card IS in. Nothing here read the card.
    """
    if not outcome.board_has_no_column:
        return ""
    return (
        f" Its board has no column called {said}, so the card was left where it was - "
        "rename a column to match, or move it by hand."
    )


def _not_a_status(typed: str) -> str:
    """What to say to something that is not a column and not one of our own names.

    Names this bot's four, because those work on every board and in every server, and a person
    whose picker came back empty - a board that would not read, three seconds gone - has otherwise
    been handed a text box and no vocabulary.
    """
    return (
        f"{typed.strip()!r} is not a status this bot knows. Pick one from the list, or use "
        f"{', '.join(OWN_NAMES[:-1])} or {OWN_NAMES[-1]}."
    )


def _suggesting(columns: OffersColumns) -> Suggesting:
    """The picker: this server's board columns first, then this bot's own names.

    The board's own columns first because they are what somebody is looking at - a board carrying
    `In progress` and `In review` has two columns this bot reads as one status, and picking between
    them is the whole reason this is a picker rather than a list of four.

    This bot's own names behind them, never instead: they are what works when a board will not read
    inside the three seconds Discord allows, and what works for a server mirroring no board at all.
    A column this bot cannot read as a status is left out, because offering one would be offering an
    entry its own callback refuses.

    Never raises, and answers nothing rather than badly. An autocomplete that fails shows the person
    nothing at all, so a GitHub outage is indistinguishable from a board with no columns - and there
    is nowhere here to put a refusal.
    """

    async def suggest(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        offered: tuple[str, ...] = ()
        if interaction.guild_id is not None:
            try:
                offered = await columns.offered(interaction.guild_id)
            except Exception:
                logger.warning("could not suggest board columns for %s", interaction.guild_id)

        # The board's own vocabulary wins, and by STATUS rather than by spelling. A board
        # calling it `Ready` where this bot says `Not reviewed` means the same thing, and
        # offering both is two entries doing one job with only one of them written on the board
        # somebody is looking at. So this bot's own name is offered only for a status the board
        # has no column for at all - which is what keeps `/status Done` available on a board
        # with nowhere to put it, so the answer is the sentence saying so rather than silence.
        on_the_board = [name for name in offered if status_from_column(name) is not None]
        covered = {status_from_column(name) for name in on_the_board}
        names = [
            *on_the_board,
            *(name for name in OWN_NAMES if status_from_column(name) not in covered),
        ]

        wanted = current.strip().casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in names
            # Discord caps a choice's value at a hundred characters and rejects the whole list
            # over it, which would take the picker down rather than one entry. A board column may
            # be longer: the column this bot stores is a hundred and twenty-eight wide.
            if wanted in name.casefold() and len(name) <= MOST_CHARACTERS
        ][:MOST_CHOICES]

    return suggest
