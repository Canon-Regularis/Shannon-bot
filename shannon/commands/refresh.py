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
from shannon.services.sync.refresh import RefreshOutcome, RefreshScope

logger = logging.getLogger(__name__)

# What the counts are counting, in the reply. Read from a table rather than branched over, so the
# three scopes cannot drift into saying different shapes of thing about the same numbers.
_KINDS = {
    RefreshScope.EVERYTHING: "open items",
    RefreshScope.PULL_REQUESTS: "open pull requests",
    RefreshScope.ISSUES: "open issues",
}

# Discord shows the description, and the value is what reaches the callback.
#
# `all` offers nothing this command could not already do: leaving the argument out has always
# meant both kinds. It is here because nothing in the picker said so, and a capability nobody can
# see is one nobody uses. Issue #92.
#
# First, because Discord shows them in the order they are written and this is the one the command
# does when nobody chooses. Built from the enum rather than from the string beside it, so a value
# no scope has cannot be offered: the callback turns whatever arrives straight back into a member.
_CHOICES = [
    app_commands.Choice(name="all", value=RefreshScope.EVERYTHING.value),
    app_commands.Choice(name="pull requests", value=RefreshScope.PULL_REQUESTS.value),
    app_commands.Choice(name="issues", value=RefreshScope.ISSUES.value),
]

# Said only where something was mirrored. It is the one surprising thing about this command, and
# it is what lets somebody run it against a real backlog without wondering who they just woke up.
#
# It covers the blocks as well as the lines. A block that is posted notifies everybody it
# mentions, so the sync services behind this are built to write names in plain text; the sentence
# was false for as long as they were not.
_QUIET = "Nobody was pinged."


class RefreshesARepository(Protocol):
    """Mirroring every open item that has no thread yet."""

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome: ...


def build_refresh_command(service: RefreshesARepository, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="refresh", description="Open threads for any GitHub items that do not have one"
    )
    # `scope` rather than `only`, which was honest with two entries and became a contradiction at
    # the third: `only: all` says the opposite of what it does.
    #
    # Renaming an option Discord has already registered is safe here for one reason, and it is
    # worth writing down because it stops being true the moment somebody makes this required. A
    # stale registration sends `only`; discord.py looks for `scope`, does not find it, and takes
    # the default, which is all of them. Required, the same line raises `CommandSignatureMismatch`
    # and the interaction dies with a generic error instead.
    #
    # "kinds of item" is load-bearing in the description. It is the only place in Discord that
    # stops `all` being read as "including the ones that already have a thread", which is a
    # different feature that this command deliberately does not do.
    @app_commands.describe(scope="Which kinds of item to cover; leaving it out is the same as all")
    @app_commands.choices(scope=_CHOICES)
    @app_commands.guild_only()
    async def refresh(
        interaction: discord.Interaction, scope: app_commands.Choice[str] | None = None
    ) -> None:
        if interaction.guild_id is None:
            await reply(interaction, "Run this inside a server channel.")
            return
        if not gate.allows(interaction.user, SYNC_ROLES):
            await reply(interaction, gate.denial("refresh", SYNC_ROLES))
            return

        # The parameter and the scope share a name because they are one fact twice over: Discord
        # hands across a choice object and the service wants the value inside it. A second name
        # for the unwrapped form would be a second thing to keep straight, for one line.
        scope = RefreshScope.EVERYTHING if scope is None else RefreshScope(scope.value)

        await defer(interaction)
        try:
            outcome = await service.refresh(guild_id=interaction.guild_id, scope=scope)
        except ShannonError as error:
            # `repository` rather than a kind, and it reads correctly in every row of the table
            # this can reach: the failures that get here are about the repository or about GitHub,
            # never about one item, because an item that fails is counted rather than raised.
            logger.warning("/refresh could not finish: %s", error.message)
            await reply(interaction, reply_for(error, noun="repository"))
        else:
            await reply(interaction, _said(outcome, _KINDS[scope]))

    return refresh


def _said(outcome: RefreshOutcome, kind: str) -> str:
    """The counts, as a sentence somebody can act on.

    `left` is the number that matters and it is always named when it is not zero, because it is
    the difference between "done" and "run it again". The failures are inside it rather than
    beside it, and are mentioned separately only so nobody reads a shortfall as a miscount.
    """
    if outcome.mirrored == 0 and outcome.left == 0:
        if outcome.already == 0:
            return f"{outcome.full_name} has no {kind} right now, so there was nothing to mirror."
        return (
            f"Nothing to mirror. All {outcome.already} {kind} on {outcome.full_name} already "
            "have a thread."
        )

    said = (
        f"Mirrored {outcome.mirrored} {kind} from {outcome.full_name}, and left alone the "
        f"{outcome.already} that already had a thread."
    )
    if outcome.left:
        # Deliberately not explaining whether the cap or a failure left them. Both mean the same
        # thing to whoever is reading: there is more to do and running it again does it.
        is_are = "is" if outcome.left == 1 else "are"
        said += f" {outcome.left} {is_are} still untracked, so run /refresh again to carry on."
    if outcome.failed:
        said += (
            f" {outcome.failed} could not be mirrored just now and are among those still "
            "untracked; the log says why."
        )
    return f"{said} {_QUIET}"
