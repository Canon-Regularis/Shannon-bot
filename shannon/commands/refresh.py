from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._cooldowns import Cooldown, NotYet, seconds_in_words
from shannon.commands._guards import in_a_server, its_turn
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
    # No "open" on these two: a draft card has no GitHub state to be open in. The only other thing
    # its row ever says is where the card went off the board (issue #198), and a card there is not
    # offered at all, so "open" would be reporting a field nobody reads.
    RefreshScope.TICKETS: ("ticket", "tickets"),
}

# What to add when a run asked about tickets and could not reach them. Each names the command that
# puts it right, because that is the only part the person can act on. A derived-set test holds this
# complete, the same way one holds `_KINDS`.
_MISSED = {
    MissedTickets.NO_BOARD: "No board is linked to this server, so tickets were not covered. "
    "Run /board link to include them.",
    MissedTickets.NO_CHANNEL: "No channel is mapped for this board's tickets, so they were not "
    "covered. Run /set_channel to include them.",
    MissedTickets.UNREADABLE: "The board could not be read, so tickets were not covered. "
    "/board show says whose authorisation it is read with, and the log says what went wrong.",
}

# Discord shows the description, the value is what reaches the callback, and the choices appear
# in the order they are written, so the default scope comes first.
_CHOICES = [
    app_commands.Choice(name="all", value=RefreshScope.EVERYTHING.value),
    app_commands.Choice(name="pull requests", value=RefreshScope.PULL_REQUESTS.value),
    app_commands.Choice(name="issues", value=RefreshScope.ISSUES.value),
    app_commands.Choice(name="tickets", value=RefreshScope.TICKETS.value),
]

# True because the sync services behind this have no notifier, and every block they write
# reaches Discord with an empty allow-list: it names linked people as real mentions, and a block
# that is posted rings none of them. Issue #185 moved it off plain-text names.
_QUIET = "Nobody was pinged."

# One run per server at a time, and the next a minute after the last one finished. Issue #185:
# a run sweeps the whole repository, so asking for one twice in a row costs everything twice.
# The words below say "a minute", so a test holds this at sixty.
REFRESH_SPACING_SECONDS = 60.0


class RefreshesARepository(Protocol):
    """Mirroring every open item that has no thread yet, and redrawing the open pull requests and
    issues that have one."""

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome: ...


def build_refresh_command(
    service: RefreshesARepository, gate: PermissionGate, *, cooldown: Cooldown | None = None
) -> SlashCommand:
    # Built here unless a test hands one in, so the container builds the command once and gets one
    # for the life of the process, which is what a wait between runs needs.
    turns = cooldown if cooldown is not None else Cooldown(REFRESH_SPACING_SECONDS)

    @app_commands.command(
        name="refresh",
        description=(
            "Open threads for items that lack one, and redraw open pull requests and issues"
        ),
    )
    # Leaving `scope` optional is load-bearing: a stale registration that sends an option name
    # this signature no longer has falls back to the default, where a required option would raise
    # `CommandSignatureMismatch`.
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

        if not await its_turn(interaction, turns, guild_id, _not_yet):
            return
        # Every way out finishes the run, a failure included, which starts the wait just the same.
        # The work a failed run did is spent either way, and a run that never ended would hold
        # the server until a restart.
        try:
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
        finally:
            turns.finish(guild_id)

    # `app_commands.command()` leaves the command's binding type unknown; `discord_bot/slash.py`
    # says why `Any` is the only truthful thing to put in.
    return refresh  # pyright: ignore[reportUnknownVariableType]


def _not_yet(wait: NotYet) -> str:
    """Why a run may not start yet, for the amber answer `its_turn` gives."""
    if wait.running:
        return "/refresh is already running in this server. Try again a minute after it finishes."
    return (
        "/refresh ran in this server less than a minute ago. Try again in "
        f"{seconds_in_words(wait.seconds)}."
    )


def _said(outcome: RefreshOutcome, kinds: tuple[str, str]) -> str:
    """The counts, and what could not be reached, as one sentence somebody can act on.

    Assembled in parts rather than as branches inside one function, because the opening half has
    four exits and three of them are the nothing-to-open cases, which since issue #185 can still
    be followed by redraws. Every part after the first starts with its own space.

    "Nobody was pinged." follows anything that wrote to a thread, and nothing else. The note about
    tickets comes last, after it: `/refresh all` on a quiet repository with no board can have no
    numbers moved at all, and then the reason is the thing worth saying.
    """
    said = _opened(outcome, kinds) + _redrawn(outcome, kinds)
    if outcome.mirrored or outcome.redrawn or outcome.rebuilt:
        said += f" {_QUIET}"
    if outcome.tickets_missed is not None:
        said += f" {_MISSED[outcome.tickets_missed]}"
    return said


def _opened(outcome: RefreshOutcome, kinds: tuple[str, str]) -> str:
    """The threads it opened, and the ones it has still to open.

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
        f"{outcome.full_name}."
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
    return said


def _redrawn(outcome: RefreshOutcome, kinds: tuple[str, str]) -> str:
    """What happened to the threads that already existed. Issue #185.

    Each part only when its count is above nought, so an ordinary run says one short sentence. The
    note about tickets carries no number, so it reads the same under `all` as under `tickets`.
    """
    one, many = kinds
    said = ""
    if outcome.redrawn:
        noun = one if outcome.redrawn == 1 else many
        said += f" Redrew {outcome.redrawn} {noun} from GitHub."
    if outcome.rebuilt == 1:
        said += f" 1 {one} had lost its thread, so it has a new one."
    elif outcome.rebuilt:
        said += f" {outcome.rebuilt} {many} had lost their threads, so they have new ones."
    if outcome.unreached:
        noun, is_are, them = (one, "is", "it") if outcome.unreached == 1 else (many, "are", "them")
        said += (
            f" {outcome.unreached} {noun} {is_are} still to be redrawn; the next /refresh with "
            f"the same scope starts with {them}."
        )
    if outcome.redraws_failed:
        noun = one if outcome.redraws_failed == 1 else many
        said += f" {outcome.redraws_failed} {noun} could not be redrawn just now; the log says why."
    if outcome.left_to_the_board:
        said += (
            " Tickets that already have a thread are left alone, because the board keeps those "
            "up to date itself."
        )
    return said
