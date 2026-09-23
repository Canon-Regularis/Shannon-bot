"""The four commands that put somebody on this thread's item, or take them off.

Run in the item's own thread, where Discord's `channel_id` is the thread id. GitHub keeps
assignees and reviewers as two lists and a person can be on both; only a pull request can be
asked for a review, so an issue refuses and says which command to use.
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
from shannon.domain.enums import ActorRole
from shannon.domain.errors import ShannonError
from shannon.services.people import PeopleOutcome

logger = logging.getLogger(__name__)


class PutsSomebodyOnAnItem(Protocol):
    """Changing who is on the item a thread belongs to, in either of its two roles."""

    async def assign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome: ...

    async def unassign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome: ...

    async def request_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome: ...

    async def unrequest_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome: ...


def build_assign_command(service: PutsSomebodyOnAnItem, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="assign",
        description="Put someone on this item as an assignee",
    )
    @app_commands.describe(member="Who to put on this item")
    @app_commands.guild_only()
    async def assign(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "assign", gate, member, service.assign)

    # discord.py's decorator leaves the binding parameter unsolved for a module-level command, so
    # it hands back `Command[Unknown, ...]` whatever this is declared as. See `discord_bot/slash`.
    return assign  # pyright: ignore[reportUnknownVariableType]


def build_unassign_command(service: PutsSomebodyOnAnItem, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(name="unassign", description="Take someone off this item's assignees")
    @app_commands.describe(member="Who to take off this item")
    @app_commands.guild_only()
    async def unassign(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "unassign", gate, member, service.unassign)

    return unassign  # pyright: ignore[reportUnknownVariableType]


def build_request_review_command(
    service: PutsSomebodyOnAnItem, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(
        name="request_review", description="Ask someone to review this pull request"
    )
    @app_commands.describe(member="Who to ask for a review")
    @app_commands.guild_only()
    async def request_review(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "request_review", gate, member, service.request_review)

    return request_review  # pyright: ignore[reportUnknownVariableType]


def build_unrequest_review_command(
    service: PutsSomebodyOnAnItem, gate: PermissionGate
) -> SlashCommand:
    @app_commands.command(
        name="unrequest_review", description="Withdraw a review request on this pull request"
    )
    @app_commands.describe(member="Whose review request to withdraw")
    @app_commands.guild_only()
    async def unrequest_review(interaction: discord.Interaction, member: discord.Member) -> None:
        await _act(interaction, "unrequest_review", gate, member, service.unrequest_review)

    return unrequest_review  # pyright: ignore[reportUnknownVariableType]


class _Change(Protocol):
    """Any of the service's four methods, which take and answer the same things."""

    async def __call__(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome: ...


async def _act(
    interaction: discord.Interaction,
    name: str,
    gate: PermissionGate,
    member: discord.Member,
    call: _Change,
) -> None:
    """The half all four commands share: check, defer, call, answer.

    Nothing is posted into the thread: GitHub sends the change back as a delivery, and the
    ordinary mirror rewrites the block and says who was asked, exactly once.
    """
    where = await in_a_thread(interaction, name, gate, SYNC_ROLES)
    if where is None:
        return

    await defer(interaction)
    try:
        outcome = await call(thread_id=where.channel_id, discord_user_id=member.id)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", name, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, done(_said(outcome, member.id)))


def _said(outcome: PeopleOutcome, discord_user_id: int) -> str:
    """What happened, with the person written as the mention the command was given."""
    unproved = _UNPROVED if outcome.changed and not outcome.proved else ""
    return _did(outcome, discord_user_id) + unproved


# Said under a change that went through on a link nobody ever proved. `/link` records a login an
# admin typed and GitHub was never asked whose it is, so this may have acted on a real repository
# as somebody who has nothing to do with the person named above.
#
# A note rather than a refusal, because every link in a server predates the command that would
# fix it: refusing on the day this ships would stop `/assign` for everybody at once, which is the
# trap migration 0021 exists to remember. `SHANNON_REQUIRE_PROVED_LINKS` turns it into a refusal
# once people have had the chance.
_UNPROVED = (
    "\n-# Nobody has proved that account belongs to them, so this went out on somebody's word "
    "for it. They can run /link to settle it."
)


# What a repeat says, keyed by whether it is a review request and whether it was adding. A
# table rather than nested ifs: four sentences, four keys, and no arm that cannot be reached.
_NOTHING_CHANGED = {
    (True, True): "{who} has already been asked for a review on {item}, so nothing changed.",
    (True, False): "{who} has not been asked for a review on {item}, so nothing changed.",
    (False, True): "{who} is already on {item}, so nothing changed.",
    (False, False): "{who} is not on {item}, so nothing changed.",
}


def _did(outcome: PeopleOutcome, discord_user_id: int) -> str:
    item = f"{outcome.full_name}#{outcome.number}"
    who = f"<@{discord_user_id}>"
    if not outcome.changed:
        # A repeat is not a failure. `/label` and the status commands have always answered this
        # way; putting somebody on an item twice was the one place that answered in red (#147).
        return _NOTHING_CHANGED[outcome.role is ActorRole.REVIEWER, outcome.added].format(
            who=who, item=item
        )
    if outcome.role is ActorRole.REVIEWER:
        if outcome.added:
            return f"Asked {who} for a review on {item}."
        return f"Withdrew the review request from {who} on {item}."
    if outcome.added:
        return f"Put {who} on {item}."
    return f"Took {who} off {item}."
