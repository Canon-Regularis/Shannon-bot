from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.sync.refresh import MissedTickets, RefreshOutcome, RefreshScope

logger = logging.getLogger(__name__)

# What the counts are counting, in the reply, singular and plural. Both, because every one
# of these is used after a number and "Mirrored 1 open pull requests" was reachable (#147).
_KINDS = {
    RefreshScope.EVERYTHING: ("open item", "open items"),
    RefreshScope.PULL_REQUESTS: ("open pull request", "open pull requests"),
    RefreshScope.ISSUES: ("open issue", "open issues"),
    # No "open" on these two: a draft card has no state to be open in, and `TicketPolicy` holds
    # every one of them at "open" for ever. Calling them open items would be reporting a field
    # nobody can change.
    RefreshScope.TICKETS: ("ticket", "tickets"),
}

# What to add when a run asked about tickets and could not reach them. Each names the command that
# puts it right, because that is the only part the person can act on. A derived-set test holds this
# complete, the same way one holds `_KINDS`.
_MISSED = {
    MissedTickets.NO_BOARD: "No board is linked to this server, so tickets were not covered. "
    "Run /set_board to include them.",
    MissedTickets.NO_CHANNEL: "No channel is mapped for this board's tickets, so they were not "
    "covered. Run /set_channel to include them.",
    MissedTickets.UNREADABLE: "The board could not be read, so tickets were not covered. "
    "The log says what GitHub answered.",
}

# Discord shows the description, the value is what reaches the callback, and the choices appear
# in the order they are written, so the default scope comes first.
_CHOICES = [
    app_commands.Choice(name="all", value=RefreshScope.EVERYTHING.value),
    app_commands.Choice(name="pull requests", value=RefreshScope.PULL_REQUESTS.value),
    app_commands.Choice(name="issues", value=RefreshScope.ISSUES.value),
    app_commands.Choice(name="tickets", value=RefreshScope.TICKETS.value),
]

# True only because the sync services behind this write names in plain text: a block that is
# posted notifies everybody it mentions.
_QUIET = "Nobody was pinged."


class RefreshesARepository(Protocol):
    """Mirroring every open item that has no thread yet."""

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome: ...


def build_refresh_command(service: RefreshesARepository, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="refresh", description="Open threads for any GitHub items that do not have one"
    )
    # Leaving `scope` optional is load-bearing: a stale registration that sends an option name
    # this signature no longer has falls back to the default, where a required option would raise
    # `CommandSignatureMismatch`. "kinds of item" stops `all` being read as including the ones
    # that already have a thread.
    @app_commands.describe(scope="Which kinds of item to cover; leaving it out is the same as all")
    @app_commands.choices(scope=_CHOICES)
    @app_commands.guild_only()
    async def refresh(
        interaction: discord.Interaction, scope: app_commands.Choice[str] | None = None
    ) -> None:
        guild_id = await in_a_server(interaction, "refresh", gate, SYNC_ROLES)
        if guild_id is None:
            return

        wanted = RefreshScope.EVERYTHING if scope is None else RefreshScope(scope.value)

        await defer(interaction)
        try:
            outcome = await service.refresh(guild_id=guild_id, scope=wanted)
        except ShannonError as error:
            # `repository` rather than a kind: an item that fails is counted rather than
            # raised, so what reaches here is about the repository or about GitHub.
            logger.warning("/refresh could not finish: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(interaction, done(_said(outcome, _KINDS[wanted])))

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # says why `Any` is the only truthful thing to put in.
    return refresh  # pyright: ignore[reportUnknownVariableType]


def _said(outcome: RefreshOutcome, kinds: tuple[str, str]) -> str:
    """The counts, and what could not be reached, as one sentence somebody can act on.

    A wrapper around `_counts` rather than another branch inside it, because `_counts` has four
    exits and three of them are the nothing-to-do cases. `/refresh all` on a quiet repository
    with no board takes one of those, and that is exactly the run where the note matters most: no
    numbers moved, and the reason is the thing worth saying.
    """
    said = _counts(outcome, kinds)
    if outcome.tickets_missed is None:
        return said
    # After "Nobody was pinged." on the did-work branch, which is deliberate. Threading it in
    # before would mean repeating the zero-work condition in two places that have to agree, and
    # two places that have to agree is how the singular-plural bug got in (#147).
    return f"{said} {_MISSED[outcome.tickets_missed]}"


def _counts(outcome: RefreshOutcome, kinds: tuple[str, str]) -> str:
    """The numbers alone.

    The failures are inside `left` rather than beside it, and are named separately only so nobody
    reads a shortfall as a miscount.

    Every number here is followed by the thing it counts, so both spellings are needed: one of
    each was reachable and read "Mirrored 1 open pull requests" (#147).
    """
    one, many = kinds
    if outcome.mirrored == 0 and outcome.left == 0:
        if outcome.already == 0:
            return f"{outcome.full_name} has no {many} right now, so there was nothing to mirror."
        if outcome.already == 1:
            return f"Nothing to mirror. The one {one} on {outcome.full_name} already has a thread."
        return (
            f"Nothing to mirror. All {outcome.already} {many} on {outcome.full_name} already "
            "have a thread."
        )

    said = (
        f"Mirrored {outcome.mirrored} {one if outcome.mirrored == 1 else many} from "
        f"{outcome.full_name}, and left alone the {outcome.already} that already had a thread."
    )
    if outcome.left:
        is_are = "is" if outcome.left == 1 else "are"
        said += f" {outcome.left} {is_are} still untracked, so run /refresh again to carry on."
    if outcome.failed:
        is_are = "is" if outcome.failed == 1 else "are"
        said += (
            f" {outcome.failed} could not be mirrored just now and {is_are} among those "
            "still untracked; the log says why."
        )
    return f"{said} {_QUIET}"
