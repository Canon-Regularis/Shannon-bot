"""`/label` and `/unlabel`: the tags that are not a workflow move.

Both run in the item's own thread and neither posts into it: GitHub sends the change back as a
`labeled` delivery and the mirror announces it. discord.py documents that autocomplete choices
are only suggestions, so the service's check, not the picker, stops a typo becoming a new label.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

import discord
from discord import app_commands

from shannon.commands._guards import DecidesGitHubAccess, github_allows, in_a_thread
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.github import people
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Discord's cap; it sends a longer list back as an error rather than truncating it.
MOST_CHOICES = 25

# What discord.py wants of an autocomplete: a coroutine taking the interaction and what has been
# typed so far. The two `Any`s are the coroutine's send and throw types, which nothing supplies.
Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]


class SetsLabels(Protocol):
    """Moving one ordinary label on the item a thread belongs to."""

    async def set_label(self, *, thread_id: int, name: str, adding: bool) -> WorkflowOutcome: ...


class SuggestsLabels(Protocol):
    """Which labels the repository behind a thread has, for the picker beside the name.

    Separate from `SetsLabels` because the picker is asked on every keystroke by somebody who has
    run nothing, and a handle that can suggest a name should not also be able to write one.
    """

    async def labels_for_thread(self, thread_id: int) -> tuple[str, ...]: ...


def build_label_command(
    service: SetsLabels,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    suggestions: SuggestsLabels,
) -> SlashCommand:
    @app_commands.command(name="label", description="Put a label on this item")
    @app_commands.describe(name="Which label, from the ones this repository has")
    @app_commands.guild_only()
    async def label(interaction: discord.Interaction, name: str) -> None:
        await _act(interaction, "label", gate, access, name, service, adding=True)

    label.autocomplete("name")(_suggesting(suggestions))
    # discord.py's decorator leaves the binding parameter unsolved for a module-level command,
    # so what it hands back is `Command[Unknown, ...]` whatever this is declared as. See
    # `discord_bot/slash.py` for why `Any` is the only truthful thing in that slot.
    return label  # pyright: ignore[reportUnknownVariableType]


def build_unlabel_command(
    service: SetsLabels,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    suggestions: SuggestsLabels,
) -> SlashCommand:
    @app_commands.command(name="unlabel", description="Take a label off this item")
    @app_commands.describe(name="Which label to remove")
    @app_commands.guild_only()
    async def unlabel(interaction: discord.Interaction, name: str) -> None:
        await _act(interaction, "unlabel", gate, access, name, service, adding=False)

    unlabel.autocomplete("name")(_suggesting(suggestions))
    return unlabel  # pyright: ignore[reportUnknownVariableType]


def _suggesting(suggestions: SuggestsLabels) -> Suggesting:
    """The picker, which must answer quickly and must never raise.

    Discord allows an autocomplete about three seconds and shows nothing at all if the callback
    fails, so a GitHub outage is indistinguishable from a repository with no labels. Not gated:
    an autocomplete cannot reply, so there is nowhere to put a refusal, and a repository's label
    names are already in every thread this bot writes.
    """

    async def suggest(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.channel_id is None:
            return []
        try:
            names = await suggestions.labels_for_thread(interaction.channel_id)
        except Exception:
            logger.warning("could not suggest labels for thread %s", interaction.channel_id)
            return []

        wanted = current.strip().casefold()
        matching = [name for name in names if wanted in name.casefold()]
        return [app_commands.Choice(name=name, value=name) for name in matching[:MOST_CHOICES]]

    return suggest


async def _act(
    interaction: discord.Interaction,
    command: str,
    gate: PermissionGate,
    access: DecidesGitHubAccess,
    name: str,
    service: SetsLabels,
    *,
    adding: bool,
) -> None:
    """The half both commands share: check, defer, call, answer.

    The picker beside the name stays ungated. An autocomplete cannot reply, so there is
    nowhere to put a refusal, and it is asked once per keystroke - gating it would be a
    GitHub call per letter typed.
    """
    where = await in_a_thread(interaction, command, gate, SYNC_ROLES)
    if where is None:
        return

    await defer(interaction)
    if not await github_allows(interaction, access, where.guild_id, at_least=people.WRITE):
        return
    try:
        outcome = await service.set_label(thread_id=where.channel_id, name=name, adding=adding)
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", command, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, done(_said(outcome, adding=adding)))


def _said(outcome: WorkflowOutcome, *, adding: bool) -> str:
    """What happened, naming the label as the repository spells it.

    `/label BUG` on a repository that has `bug` writes `bug`, and echoing what was typed would
    hide that.
    """
    item = f"{outcome.full_name}#{outcome.number}"
    if not outcome.changed:
        held = "already has" if adding else "does not have"
        return f"{item} {held} the label `{outcome.label}`, so nothing changed."
    return f"{'Put' if adding else 'Took'} `{outcome.label}` {'on' if adding else 'off'} {item}."
