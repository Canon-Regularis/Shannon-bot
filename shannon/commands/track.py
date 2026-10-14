"""`/track`: which kinds of line this thread gets. Issue #163.

Besides its block, which is always kept current, an item's thread gets a line for things that
happen to the item. Most ring nobody, and in a busy repository they bury the conversation the
thread is for. This turns one kind on or off for the thread it is run in; with no kind it lists
every kind the item can get and where each stands, and with only a kind it says where that one
stands.

The tier every other command run inside a thread takes, because it changes what everybody in
that thread is told. The answer is private and the thread is not told: a line saying the lines
had changed would be the very noise this exists to take away, and anybody who wonders why a line
stopped can ask the same command.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_thread
from shannon.commands._permissions import SYNC_ROLES
from shannon.commands._replies import reply_for
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import ObjectType
from shannon.domain.errors import ShannonError
from shannon.domain.tracking import HAPPENS_TO, TrackedEvent
from shannon.services.tracking import ThreadLines

logger = logging.getLogger(__name__)

NO_KIND = "Pick which kind of line to turn on or off, or leave both out to see every kind."

# Named as they are stored, so what the README and the issue say is what somebody types.
_EVENTS = [app_commands.Choice(name=event.value, value=event.value) for event in TrackedEvent]
# Discord shows the name; the value is what reaches the callback.
_STATES = [
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
]

# What each kind covers for each kind of item, in the words somebody reading the thread would
# use. Keyed exactly as `HAPPENS_TO`, which a test holds.
_COVERS: dict[ObjectType, dict[TrackedEvent, str]] = {
    ObjectType.PR: {
        TrackedEvent.COMMENTS: "comments and reviews",
        TrackedEvent.LABELS: "labels, statuses and priorities",
        TrackedEvent.COMMITS: "commits and force pushes",
        TrackedEvent.CHECKS: "CI results",
        TrackedEvent.APPROVALS: "everybody having approved",
        TrackedEvent.DRAFT: "leaving draft and going back into it",
        TrackedEvent.PEOPLE: "review requests and assignments",
        TrackedEvent.STATE: "closing, merging and reopening",
    },
    ObjectType.ISSUE: {
        TrackedEvent.COMMENTS: "comments",
        TrackedEvent.LABELS: "labels, statuses and priorities",
        TrackedEvent.PEOPLE: "assignments",
        TrackedEvent.STATE: "closing and reopening",
    },
    ObjectType.TICKET: {
        TrackedEvent.LABELS: "the card's fields changing",
        TrackedEvent.STATE: "the card being archived, restored, deleted or converted",
    },
}

# What still happens once a kind is off, where that is less than it may sound like. Keyed as
# `_COVERS` is, leaving out a kind with nothing to add.
_SHUTS_ALL_THE_SAME = " The thread still shuts and opens again as it did, without a line saying so"
_UNLESS_REFUSED = f"{_SHUTS_ALL_THE_SAME}, unless this bot is not allowed to shut it."
_NOBODY_LATER = " Nobody put on the item while this is off is rung for it later."
_STILL: dict[ObjectType, dict[TrackedEvent, str]] = {
    ObjectType.PR: {TrackedEvent.STATE: _UNLESS_REFUSED, TrackedEvent.PEOPLE: _NOBODY_LATER},
    ObjectType.ISSUE: {TrackedEvent.STATE: _UNLESS_REFUSED, TrackedEvent.PEOPLE: _NOBODY_LATER},
    # Never told about a shut it was refused, whatever it chose, so it is promised no exception.
    ObjectType.TICKET: {TrackedEvent.STATE: f"{_SHUTS_ALL_THE_SAME}."},
}

_AN: dict[ObjectType, str] = {
    ObjectType.PR: "A pull request",
    ObjectType.ISSUE: "An issue",
    ObjectType.TICKET: "A board card",
}


class ChoosesWhatAThreadIsTold(Protocol):
    """Reading and changing which lines an item's thread is told."""

    async def told(self, *, thread_id: int) -> ThreadLines: ...

    async def choose(
        self, *, thread_id: int, event: TrackedEvent, wanted: bool, by: int
    ) -> ThreadLines: ...


def build_track_command(service: ChoosesWhatAThreadIsTold, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="track", description="Which kinds of line this thread gets, or turn one on or off"
    )
    @app_commands.describe(
        event="The kind of line; leave it out to see every kind and where it stands",
        state="on to post it here, off to stop; leave it out to see where it stands",
    )
    @app_commands.choices(event=_EVENTS, state=_STATES)
    @app_commands.guild_only()
    async def track(
        interaction: discord.Interaction,
        event: app_commands.Choice[str] | None = None,
        state: app_commands.Choice[str] | None = None,
    ) -> None:
        # Both optional, as `/refresh`'s scope is, so a registration Discord still holds from
        # before a kind was added falls back to reading rather than refusing to run.
        where = await in_a_thread(interaction, "track", gate, SYNC_ROLES)
        if where is None:
            return
        if event is None and state is not None:
            await reply(interaction, refused(NO_KIND))
            return

        kind = None if event is None else TrackedEvent(event.value)
        wanted = None if state is None else state.value == "on"
        await defer(interaction)
        try:
            if kind is None or wanted is None:
                lines = await service.told(thread_id=where.channel_id)
            else:
                lines = await service.choose(
                    thread_id=where.channel_id,
                    event=kind,
                    wanted=wanted,
                    by=interaction.user.id,
                )
        except ShannonError as error:
            logger.warning("choosing which lines a thread is told failed: %s", error.message)
            await reply(interaction, reply_for(error))
            return
        await reply(interaction, _answer(lines, kind, wanted))

    # `app_commands.command()` leaves the command's binding type unknown, which
    # `discord_bot/slash.py` argues `Any` is the only truthful thing to put in.
    return track  # pyright: ignore[reportUnknownVariableType]


def _answer(lines: ThreadLines, kind: TrackedEvent | None, wanted: bool | None) -> Panel:
    """The answer to whichever of the three questions was asked.

    Reading carries no mark, as `/mentions` reading back its setting does not: nothing changed,
    and a tick on the answer to a question would be claiming something had.
    """
    if kind is None:
        return Panel.of_text(_every_kind(lines))
    covers = _COVERS[lines.kind].get(kind)
    if covers is None:
        choices = _either([f"`{each.value}`" for each in HAPPENS_TO[lines.kind]])
        return refused(
            f"{_AN[lines.kind]} never gets that kind of line. Here you can pick {choices}."
        )
    on = "on" if lines.tracking.wants(kind) else "off"
    if wanted is None:
        return Panel.of_text(f"Lines about {covers} are {on} in this thread.")
    if lines.was is wanted:
        return done(f"Lines about {covers} were already {on} in this thread.")
    said = f"Lines about {covers} are {on} in this thread from now on."
    return done(said if wanted else said + _STILL[lines.kind].get(kind, ""))


def _every_kind(lines: ThreadLines) -> str:
    """Every kind this item can get, in order, with where each stands and what to type."""
    rows = [
        f"- {_capitalised(_COVERS[lines.kind][each])}: "
        f"{'on' if lines.tracking.wants(each) else 'off'} (`{each.value}`)"
        for each in HAPPENS_TO[lines.kind]
    ]
    return "\n".join(
        ["What this thread is told:", *rows, "-# Pick a kind, and on or off, to change one."]
    )


def _capitalised(words: str) -> str:
    """The first letter up and the rest left alone, so "CI" stays "CI"."""
    return words[:1].upper() + words[1:]


def _either(names: list[str]) -> str:
    """`A or B`, `A, B or C`: what may be picked, read as a sentence. Never fewer than two."""
    return f"{', '.join(names[:-1])} or {names[-1]}"
