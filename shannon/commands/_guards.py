"""The checks a slash command makes before it does anything.

`None` means the guard has already answered the interaction and the caller must return, and so
does `False`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Protocol

import discord

from shannon.commands._cooldowns import Cooldown, NotYet
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, owed, refused, reply
from shannon.discord_bot.roles import CommandRole
from shannon.github.errors import GitHubError, GitHubRateLimitError

NOT_IN_A_SERVER = "Run this inside a server channel."
NOT_IN_A_THREAD = "Run this inside the item's thread."

# What follows the role's denial where GitHub could stand in for the role and could not be asked.
# Issue #94. Its own sentence rather than the grant's, because the grant's sentences say what GitHub
# answered, and here it answered nothing.
COULD_NOT_ASK_GITHUB = (
    "GitHub could not be asked what your account may do on this server's repository just now, so "
    "only the role counted. Try again in a minute."
)


@dataclass(frozen=True, slots=True)
class InAThread:
    """Where a command was run, once both ids are known to be there."""

    guild_id: int
    channel_id: int


async def in_a_server(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
) -> int | None:
    """The guild a permitted caller ran this in, or None having said why not."""
    guild_id = interaction.guild_id
    if guild_id is None:
        await reply(interaction, refused(NOT_IN_A_SERVER))
        return None
    if not await holds_the_tier(interaction, guild_id, command, gate, roles):
        return None
    return guild_id


async def holds_the_tier(
    interaction: discord.Interaction,
    guild_id: int,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
    *,
    aside: str = "",
) -> bool:
    """Whether the caller holds one of `roles`, or GitHub stands in for one, having said why not.

    Issue #94. The role first, exactly as before: whoever holds it is through with no defer and
    no I/O, so whatever a command answers next is still its first response. Where the gate has
    no GitHub route - the server never turned it on, or nothing `roles` allows is a tier GitHub
    stands in for - the refusal is today's, word for word, and the first response too.

    Only with a route is anything else asked, and the interaction is deferred BEFORE it is. The
    question reads the database and then GitHub, and Discord drops an interaction left unanswered
    for three seconds, which says nothing at all: worse than either answer. So a refusal from here
    is a private followup rather than a first response, and the role's own denial still opens it,
    because the role is the first thing that would have let them in.

    GitHub not answering is a refusal, never a grant, since this raises access. Amber where it was
    a rate limit, which comes right on its own, and red otherwise: the split `_COMES_RIGHT` makes
    for every other reply.

    `aside` is a sentence to end every refusal on, for a command with a half that needs no role.
    The parts are joined with one space and an empty one is left out, so no refusal can end on a
    space.
    """
    if gate.allows(interaction.user, roles):
        return True
    denial = gate.denial(command, roles)
    route = gate.github_route(roles)
    if route is None:
        await reply(interaction, refused(_joined(denial, aside)))
        return False

    await defer(interaction)
    try:
        refusal = await route.refusal_for(guild_id=guild_id, discord_user_id=interaction.user.id)
    except GitHubError as unanswered:
        # Logged by the grant, which knows whose account it asked about and on what.
        said = _joined(denial, COULD_NOT_ASK_GITHUB, aside)
        comes_right = isinstance(unanswered, GitHubRateLimitError)
        await reply(interaction, owed(said) if comes_right else refused(said))
        return False
    if refusal is None:
        return True
    await reply(interaction, refused(_joined(denial, refusal, aside)))
    return False


def _joined(*parts: str) -> str:
    """The sentences that are there, one space apart."""
    return " ".join(part for part in parts if part)


async def its_turn(
    interaction: discord.Interaction,
    cooldown: Cooldown,
    key: int,
    said: Callable[[NotYet], str],
) -> bool:
    """Whether a run may start now, having said why not where it may not. Issue #185.

    After the role guard, so somebody without the role spends nobody's turn, and before `defer`:
    the answer is held in memory, so the refusal can be the interaction's first response. Amber,
    because it comes right on its own. True means the run has begun, and the caller owes
    `cooldown.finish(key)` in a `finally` that starts straight after this.

    Still after the guard where GitHub stands in for the role (issue #94), so a member GitHub
    turns away spends nobody's turn either, and no turn is held while GitHub is being asked. The
    one difference is for a member GitHub let in: the guard deferred before asking, so their amber
    arrives as a private followup rather than as the first response. A role holder's is unchanged.
    """
    not_yet = cooldown.begin(key)
    if not_yet is None:
        return True
    await reply(interaction, owed(said(not_yet)))
    return False


async def in_a_thread(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    roles: Collection[CommandRole],
) -> InAThread | None:
    """The guild and channel a permitted caller ran this in, or None having said why not.

    The channel is checked after the tier, so somebody without the role is told that instead.
    """
    guild_id = await in_a_server(interaction, command, gate, roles)
    if guild_id is None:
        return None
    if interaction.channel_id is None:
        await reply(interaction, refused(NOT_IN_A_THREAD))
        return None
    return InAThread(guild_id=guild_id, channel_id=interaction.channel_id)


class DecidesGitHubAccess(Protocol):
    """Whether the caller's GitHub account may make this change, and why not where it may not."""

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, at_least: str
    ) -> str | None: ...


async def github_allows(
    interaction: discord.Interaction,
    access: DecidesGitHubAccess,
    guild_id: int,
    *,
    at_least: str,
) -> bool:
    """Whether to go on, having already said why not where the answer is no.

    Called AFTER the interaction is deferred, never before. Discord allows three seconds for a
    first response and this makes a network call, so asking first risks an interaction that
    expires rather than one that refuses - and an expired interaction says nothing at all, which
    is the one outcome worse than either answer.

    The role check comes first, so somebody without the Discord role is told that rather than
    being told about a GitHub account they may not have connected.

    A member GitHub let in where the role did not (issue #94) has been asked about once already,
    by the tier guard. This is the second question, and it is #158's: a restriction on whoever got
    past the gate, kept as it was rather than folded into the grant, so turning the grant on
    changes nothing about it. Every level the grant gives is at least write, so for those members
    the two agree, at the cost of asking GitHub twice.
    """
    refusal = await access.refusal_for(
        guild_id=guild_id, discord_user_id=interaction.user.id, at_least=at_least
    )
    if refusal is None:
        return True
    await reply(interaction, refused(refusal))
    return False
