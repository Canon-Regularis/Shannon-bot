from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import discord
from discord import app_commands

from shannon.commands._guards import in_a_server
from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands._replies import words_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import NotRegisteredError, ShannonError
from shannon.github.projects import ProjectListing
from shannon.services.boards import BoardLink

logger = logging.getLogger(__name__)

# Discord's cap; it sends a longer list back as an error rather than truncating it.
MOST_CHOICES = 25

# What the picker offers for "stop mirroring a board": the value Discord sends when that entry is
# committed, and the label it shows. Both live here because `_wanted` accepts either, and a
# constant is what stops the two drifting apart - the parser accepting a label the picker no
# longer writes is a bug nothing would notice.
#
# A number rather than a word for the value, because it is parsed as one and no board is zero.
NONE_CHOSEN = "0"
NONE_LABEL = "None — stop mirroring a board"

Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]

# A board's own page, both kinds of owner, with whatever GitHub has appended to it - a view,
# a query, a trailing slash. Anchored at both ends so a sentence that merely CONTAINS one is
# still refused rather than half-read.
_BOARD_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/(?:users|orgs)/"
    r"(?P<owner>[^/]+)/projects/(?P<number>\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# The picker's own board entries, arriving as text. `\b` rather than anything looser, so "#6abc"
# is still refused: the boundary is what keeps this to the shape `_label_for` writes instead of
# reading a number out of any prose that happens to open with a hash.
_BOARD_LABEL = re.compile(r"^#(?P<number>\d+)\b")


@dataclass(frozen=True, slots=True)
class ChosenBoard:
    """A board somebody named, and the owner where they named one.

    The owner is empty for a bare number, which is every entry the picker offers: those are
    already listed under an owner, so repeating it would be this command telling the service
    something the service just told it.
    """

    number: int
    owner: str = ""


class LinksBoards(Protocol):
    """Pointing this server's repository at a board, and listing the ones to choose from."""

    async def assign(
        self, *, guild_id: int, project_number: int | None, typed_owner: str, acting: int
    ) -> BoardLink: ...

    async def choices_for(self, guild_id: int, typed_owner: str) -> Sequence[ProjectListing]: ...


def build_set_board_command(service: LinksBoards, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="set_board", description="Choose which GitHub project board this server mirrors"
    )
    @app_commands.describe(
        board="The board to mirror: pick one, paste its URL, or type its number",
        owner="Only if the board is not owned by this repository's own owner",
    )
    @app_commands.guild_only()
    async def set_board(interaction: discord.Interaction, board: str, owner: str = "") -> None:
        guild_id = await in_a_server(interaction, "set_board", gate, REGISTER_ROLES)
        if guild_id is None:
            return

        chosen = _wanted(board)
        if chosen is None:
            await reply(
                interaction,
                refused(
                    f"{board!r} is not a board. Pick one from the list, paste the board's "
                    "URL, or type the number off the end of it."
                ),
            )
            return

        await defer(interaction)
        try:
            link = await service.assign(
                guild_id=guild_id,
                # Zero is the picker's "stop mirroring a board", which the service spells None.
                project_number=chosen.number if chosen.number > 0 else None,
                # What was typed beats what was pasted. Somebody who filled in both meant
                # the one they typed, and silently preferring the URL would be this command
                # overruling them about the half of the address people get wrong.
                typed_owner=owner or chosen.owner,
                # Whoever ran it, and not a parameter anybody can name. Since issue #170 their
                # GitHub authorisation is what this board is read under from here on, so linking
                # one on somebody else's behalf would put a server back on one person's
                # credential - which is the thing the authorisation replaced.
                acting=interaction.user.id,
            )
        except NotRegisteredError as error:
            await reply(interaction, refused(words_for(error, noun="repository")))
            return
        except ShannonError as error:
            await reply(interaction, refused(error.message))
            return

        await reply(interaction, done(_said(link)))

    set_board.autocomplete("board")(_suggesting(service))
    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` says why `Any` is the only truthful thing to put in.
    return set_board  # pyright: ignore[reportUnknownVariableType]


def _wanted(board: str) -> ChosenBoard | None:
    """What somebody chose: a number, zero for none of them, and any owner they pasted.

    Three outcomes rather than two, and the distinction is the point: "stop mirroring a board"
    and "that is not a board" are different answers and were the same one once. Parsed rather
    than trusted, because discord.py documents a choice as a suggestion - what arrives here
    may have been typed, and typed prose must be turned away with a sentence rather than
    quietly clearing somebody's board.

    A whole URL is accepted because it is the obvious thing to paste: it is what the picker's
    entries are named after and what GitHub puts in the address bar. Refusing it and then
    asking the person to read the number off the end of that same URL was work this could do.

    And a URL carries the OWNER, which is the other half of addressing a board and the half
    people get wrong. `/users/` and `/orgs/` are the same two prefixes the board reader
    splits on, for the same reason: a login names one account of one kind.

    The picker's OWN entries are accepted, which ought to go without saying and did not. Discord
    sends a choice's value when the entry is committed and the raw text when it is typed or a
    highlighted suggestion is let fall through, and those are different strings: the value is the
    bare number, the label is "#6 Shannon Bot". So picking what this command offered was answered
    with "that is not a board. Pick one from the list" - told to somebody who just had. A label
    carries no owner, because every entry the picker offers is already listed under one.
    """
    text = board.strip()
    if text.isdigit():
        return ChosenBoard(number=int(text))
    if text == NONE_LABEL:
        # Exact, unlike the board labels above, because this is the one entry whose meaning is
        # destructive. A prefix match would put "None of them are right" a hash away from
        # unlinking somebody's board, and the em dash is what makes the exact form unmistakable.
        return ChosenBoard(number=int(NONE_CHOSEN))

    label = _BOARD_LABEL.match(text)
    if label is not None:
        return ChosenBoard(number=int(label.group("number")))

    found = _BOARD_URL.match(text)
    if found is None:
        return None
    return ChosenBoard(number=int(found.group("number")), owner=found.group("owner"))


def _suggesting(service: LinksBoards) -> Suggesting:
    """The picker, which must answer quickly and must never raise.

    Discord allows an autocomplete about three seconds and shows nothing at all when the callback
    fails, so a GitHub outage would be indistinguishable from an owner with no boards. Not gated:
    an autocomplete has nowhere to put a refusal, and a board's title is already in every thread
    this bot opens for one of its cards.
    """

    async def suggest(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        # Whatever has been typed into the other field so far. `namespace` carries only the
        # options Discord has actually sent, so this is absent as readily as it is empty.
        typed_owner = getattr(interaction.namespace, "owner", "") or ""
        try:
            listed = await service.choices_for(interaction.guild_id, typed_owner)
        except Exception:
            logger.warning("could not suggest boards for guild %s", interaction.guild_id)
            return []

        wanted = current.strip().casefold()
        matching = [
            app_commands.Choice(name=_label_for(one), value=str(one.number))
            for one in listed
            if wanted in one.title.casefold() or wanted in str(one.number)
        ]
        # Last rather than first: an owner with more than twenty-five boards would otherwise
        # spend the one slot that is always offered on the entry nobody is looking for.
        clearing = app_commands.Choice(name=NONE_LABEL, value=NONE_CHOSEN)
        return [*matching[: MOST_CHOICES - 1], clearing]

    return suggest


def _label_for(one: ProjectListing) -> str:
    """How a board is named in the picker.

    One function rather than a formatted string at the call site, because `_wanted` reads this
    shape back: a client that submits the label as text instead of committing the choice hands it
    over verbatim, and a label the parser no longer recognises refuses the entry it just offered.
    """
    return f"#{one.number} {one.title}"


def _said(link: BoardLink) -> str:
    """What changed, in one sentence, naming the board that was dropped where one was.

    A swapped board reads as having done nothing when the number was a digit out, so the old one
    is named rather than left for somebody to notice a week later.
    """
    if link.number == 0:
        if link.replaced is None:
            return f"{link.repo_name} was not mirroring a board, and still is not."
        return (
            f"{link.repo_name} has stopped mirroring board #{link.replaced}. "
            "Its threads are left where they are."
        )

    moved = (
        f" It was mirroring #{link.replaced}."
        if link.replaced is not None and link.replaced != link.number
        else ""
    )
    return (
        f"{link.repo_name} now mirrors {link.owner}'s board #{link.number}, {link.title}.{moved} "
        "Cards appear at the next poll rather than at once."
    )
