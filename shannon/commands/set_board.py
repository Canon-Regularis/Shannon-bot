from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Sequence
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

# What the picker offers for "stop mirroring a board". A number rather than a word, because the
# value is parsed as one and a board is never numbered zero.
NONE_CHOSEN = "0"

Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]


class LinksBoards(Protocol):
    """Pointing this server's repository at a board, and listing the ones to choose from."""

    async def assign(
        self, *, guild_id: int, project_number: int | None, typed_owner: str
    ) -> BoardLink: ...

    async def choices_for(self, guild_id: int, typed_owner: str) -> Sequence[ProjectListing]: ...


def build_set_board_command(service: LinksBoards, gate: PermissionGate) -> SlashCommand:
    @app_commands.command(
        name="set_board", description="Choose which GitHub project board this server mirrors"
    )
    @app_commands.describe(
        board="The board to mirror, or None to stop mirroring one",
        owner="Only if the board is not owned by this repository's own owner",
    )
    @app_commands.guild_only()
    async def set_board(interaction: discord.Interaction, board: str, owner: str = "") -> None:
        guild_id = await in_a_server(interaction, "set_board", gate, REGISTER_ROLES)
        if guild_id is None:
            return

        wanted = _wanted(board)
        if wanted is None:
            await reply(
                interaction,
                refused(
                    f"{board!r} is not a board number. Pick one from the list, or type the "
                    "number out of the board's URL."
                ),
            )
            return

        await defer(interaction)
        try:
            link = await service.assign(
                guild_id=guild_id,
                # Zero is the picker's "stop mirroring a board", which the service spells None.
                project_number=wanted if wanted > 0 else None,
                typed_owner=owner,
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


def _wanted(board: str) -> int | None:
    """The number somebody chose, zero for none of them, or None for something unreadable.

    Three outcomes rather than two, and the distinction is the point: "stop mirroring a board"
    and "that is not a board number" are different answers and used to be the same one. Parsed
    rather than trusted, because discord.py documents a choice as a suggestion - what arrives
    here may have been typed, and typed prose must be turned away with a sentence rather than
    quietly clearing somebody's board.
    """
    text = board.strip()
    return int(text) if text.isdigit() else None


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
            app_commands.Choice(name=f"#{one.number} {one.title}", value=str(one.number))
            for one in listed
            if wanted in one.title.casefold() or wanted in str(one.number)
        ]
        # Last rather than first: an owner with more than twenty-five boards would otherwise
        # spend the one slot that is always offered on the entry nobody is looking for.
        clearing = app_commands.Choice(name="None — stop mirroring a board", value=NONE_CHOSEN)
        return [*matching[: MOST_CHOICES - 1], clearing]

    return suggest


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
