"""`/label` and `/unlabel`: the tags that are not a workflow move.

Issue #104. The eight `/set_*` commands write the five status labels and the three priority ones,
and until now nothing could put an ordinary one on an item. These do: `good first issue`, `bug`,
`documentation`.

Run inside the item's own thread and take the label's name. Neither posts into the thread, because
GitHub sends the change back as a `labeled` delivery and the ordinary mirror already announces it.

The first commands in this project with an autocomplete, which is worth knowing before reading the
callback below: discord.py documents that the choices it returns are suggestions, and that somebody
may ignore them and type whatever they like. So the picker is a convenience and the service's own
check is the thing that stops a typo becoming a new label on the repository. They are not
alternatives to one another.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

import discord
from discord import app_commands

from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.workflow import WorkflowOutcome

logger = logging.getLogger(__name__)

# Discord will not show more than this many, and sends the list back as an error rather than
# truncating it for us.
MOST_CHOICES = 25

# What discord.py wants of an autocomplete: a coroutine function taking the interaction and
# what has been typed so far. The two `Any`s are the coroutine's own send and throw types,
# which nothing here or anywhere else ever supplies.
Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]


class SetsLabels(Protocol):
    """Moving one ordinary label on the item a thread belongs to."""

    async def set_label(self, *, thread_id: int, name: str, adding: bool) -> WorkflowOutcome: ...


class SuggestsLabels(Protocol):
    """Which labels the repository behind a thread has, for the picker beside the name.

    Its own protocol rather than a second method on the one above, because the picker is asked on
    every keystroke by somebody who has not run anything yet, and a handle that can suggest a name
    should not also be able to write one.
    """

    async def labels_for_thread(self, thread_id: int) -> tuple[str, ...]: ...


def build_label_command(
    service: SetsLabels, gate: PermissionGate, suggestions: SuggestsLabels
) -> SlashCommand:
    @app_commands.command(name="label", description="Put a label on this item")
    @app_commands.describe(name="Which label, from the ones this repository has")
    @app_commands.guild_only()
    async def label(interaction: discord.Interaction, name: str) -> None:
        await _act(interaction, "label", gate, name, service, adding=True)

    label.autocomplete("name")(_suggesting(suggestions))
    # discord.py's decorator leaves the binding parameter unsolved for a module-level
    # command, so the object it hands back is `Command[Unknown, ...]` whatever this is
    # declared as. `discord_bot/slash.py` argues why `Any` is the only truthful thing to put
    # in that slot; this silences pyright noticing the same gap a second time.
    return label  # pyright: ignore[reportUnknownVariableType]


def build_unlabel_command(
    service: SetsLabels, gate: PermissionGate, suggestions: SuggestsLabels
) -> SlashCommand:
    @app_commands.command(name="unlabel", description="Take a label off this item")
    @app_commands.describe(name="Which label to remove")
    @app_commands.guild_only()
    async def unlabel(interaction: discord.Interaction, name: str) -> None:
        await _act(interaction, "unlabel", gate, name, service, adding=False)

    unlabel.autocomplete("name")(_suggesting(suggestions))
    return unlabel  # pyright: ignore[reportUnknownVariableType]


def _suggesting(suggestions: SuggestsLabels) -> Suggesting:
    """The picker, which must answer quickly and must never raise.

    Discord allows an autocomplete about three seconds and shows nothing at all if the callback
    fails, so a GitHub outage would leave an empty box with no way to tell that apart from a
    repository with no labels. Swallowed and logged for that reason: the field still accepts a
    typed name, and the command behind it still checks one.

    Not gated. An autocomplete cannot reply, so there is nowhere to put a refusal, and the names
    of a repository's labels are already in every thread this bot writes.
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
    name: str,
    service: SetsLabels,
    *,
    adding: bool,
) -> None:
    """The half both commands share: check, defer, call, answer."""
    if interaction.guild_id is None:
        await reply(interaction, "Run this inside a server channel.")
        return
    if not gate.allows(interaction.user, SYNC_ROLES):
        await reply(interaction, gate.denial(command, SYNC_ROLES))
        return
    if interaction.channel_id is None:
        await reply(interaction, "Run this inside the item's thread.")
        return

    await defer(interaction)
    try:
        outcome = await service.set_label(
            thread_id=interaction.channel_id, name=name, adding=adding
        )
    except ShannonError as error:
        logger.warning("/%s could not finish: %s", command, error.message)
        await reply(interaction, reply_for(error))
    else:
        await reply(interaction, done(_said(outcome, adding=adding)))


def _said(outcome: WorkflowOutcome, *, adding: bool) -> str:
    """What happened, naming the label as the repository spells it.

    Not as it was typed. `/label BUG` on a repository that has `bug` writes `bug`, and saying back
    what somebody typed would hide the one thing about that worth showing them.
    """
    item = f"{outcome.full_name}#{outcome.number}"
    if not outcome.changed:
        held = "already has" if adding else "does not have"
        return f"{item} {held} the label `{outcome.label}`, so nothing changed."
    return f"{'Put' if adding else 'Took'} `{outcome.label}` {'on' if adding else 'off'} {item}."
