"""`/set_board`, against a stub.

Issue #158. The board used to be an environment variable, so pointing a server at one meant an
operator with shell access and a restart. This is the command that replaces that, and two things
about it are worth the tests below.

A choice is a SUGGESTION. discord.py says so, and Discord will send whatever somebody typed
instead, so the number that arrives here may be prose and has to be turned away with a sentence
rather than parsed into a zero that quietly clears a board.

And "stop mirroring a board" and "that is not a board number" are different answers that a single
`int | None` collapses into one.
"""

from __future__ import annotations

from typing import cast

import discord
import pytest

from shannon.commands.set_board import (
    MOST_CHOICES,
    NONE_CHOSEN,
    _suggesting,
    _wanted,
    build_set_board_command,
)
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import NotRegisteredError
from shannon.github.projects import ProjectListing
from shannon.services.boards import BoardLink, BoardTakenError, BoardUnreadableError
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

GUILD = 1


def link(*, number: int = 3, replaced: int | None = None) -> BoardLink:
    return BoardLink(
        repo_name="acme/widget", owner="acme", number=number, title="Roadmap", replaced=replaced
    )


class StubBoards:
    def __init__(
        self,
        *,
        result: BoardLink | None = None,
        error: Exception | None = None,
        listed: tuple[ProjectListing, ...] = (),
        listing_error: Exception | None = None,
    ) -> None:
        self.result = result or link()
        self.error = error
        self.listed = listed
        self.listing_error = listing_error
        self.calls: list[tuple[int, int | None, str]] = []

    async def assign(
        self, *, guild_id: int, project_number: int | None, typed_owner: str
    ) -> BoardLink:
        self.calls.append((guild_id, project_number, typed_owner))
        if self.error is not None:
            raise self.error
        return self.result

    async def choices_for(self, guild_id: int, typed_owner: str) -> tuple[ProjectListing, ...]:
        if self.listing_error is not None:
            raise self.listing_error
        return self.listed


def run_it(*, service: StubBoards | None = None, who=None):
    service = service or StubBoards()
    command = build_set_board_command(service, default_gate())
    return command, FakeInteraction(user=who or administrator()), service


async def fire(
    command: SlashCommand, interaction: FakeInteraction, board: str, owner: str = ""
) -> None:
    """Run the command, with discord.py's typing answered once rather than at every call.

    Two things it cannot see through: the interaction is a stand-in, so it is cast to the real
    one, and `app_commands.Command` declares its parameters as `...`, which pyright reads as a
    definite arity rather than as anything goes. One suppression with its reason beside it
    leaves this file gated rather than sitting on the ratchet.
    """
    theirs = cast(discord.Interaction, interaction)
    await command.callback(theirs, board, owner)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]


class TestWhoMayRunIt:
    @pytest.mark.parametrize("who", [project_manager, administrator])
    async def test_the_tiers_that_may(self, who) -> None:
        command, interaction, service = run_it(who=who())

        await fire(command, interaction, "3")

        assert service.calls == [(GUILD, 3, "")]

    async def test_anybody_else_is_refused_before_anything_is_written(self) -> None:
        command, interaction, service = run_it(who=developer())

        await fire(command, interaction, "3")

        assert "You need one of these roles" in interaction.reply
        assert service.calls == []

    async def test_outside_a_server(self) -> None:
        command, interaction, service = run_it()
        interaction.guild_id = None

        await fire(command, interaction, "3")

        assert interaction.said == "Run this inside a server channel."
        assert service.calls == []


class TestWhatArrivesInTheField:
    """A choice is a suggestion. Whatever Discord sends has to survive being read."""

    async def test_a_number_that_was_offered(self) -> None:
        command, interaction, service = run_it()

        await fire(command, interaction, "3")

        assert service.calls == [(GUILD, 3, "")]

    async def test_zero_clears_the_board(self) -> None:
        """The picker's own "None" entry. Spelled None to the service, because an owner with no
        number addresses nothing and would sit in the row looking like configuration."""
        command, interaction, service = run_it(service=StubBoards(result=link(number=0)))

        await fire(command, interaction, NONE_CHOSEN)

        assert service.calls == [(GUILD, None, "")]

    @pytest.mark.parametrize("typed", ["Roadmap", "", "  ", "#3", "3.0", "-1"])
    async def test_prose_is_refused_rather_than_read_as_none(self, typed: str) -> None:
        """The bug this shape exists to prevent. Parsed with a bare `int | None`, every one of
        these would have reached the service as "clear the board" and silently unlinked one."""
        command, interaction, service = run_it()

        await fire(command, interaction, typed)

        assert "is not a board number" in interaction.said
        assert service.calls == []

    async def test_a_typed_owner_is_passed_through(self) -> None:
        command, interaction, service = run_it()

        await fire(command, interaction, "3", "acme")

        assert service.calls == [(GUILD, 3, "acme")]


class TestWhenTheServiceRefuses:
    async def test_a_server_with_no_repository(self) -> None:
        """Through the reply table like every other command's, rather than around it: the same
        refusal reaching a person from two commands should read the same way from both."""
        command, interaction, _ = run_it(
            service=StubBoards(
                error=NotRegisteredError("This server has no repository yet. Run /register first.")
            )
        )

        await fire(command, interaction, "3")

        assert "/register" in interaction.said

    async def test_a_board_the_token_cannot_open(self) -> None:
        """Said to the person who typed it rather than written to a log once a minute, which is
        where an unreadable board used to surface."""
        command, interaction, _ = run_it(
            service=StubBoards(error=BoardUnreadableError("acme has no project board numbered 3"))
        )

        await fire(command, interaction, "3")

        assert "no project board numbered 3" in interaction.said

    async def test_a_board_another_repository_already_mirrors(self) -> None:
        command, interaction, _ = run_it(
            service=StubBoards(error=BoardTakenError("other/repo is already mirroring that board"))
        )

        await fire(command, interaction, "3")

        assert "already mirroring" in interaction.said


class TestWhatItSaysBack:
    async def test_a_board_it_linked(self) -> None:
        command, interaction, _ = run_it()

        await fire(command, interaction, "3")

        assert "acme's board #3, Roadmap" in interaction.said
        assert "next poll" in interaction.said, "it promised cards would appear at once"

    async def test_a_board_it_replaced_is_named(self) -> None:
        """A swapped board reads as having done nothing when the number was a digit out."""
        command, interaction, _ = run_it(service=StubBoards(result=link(replaced=9)))

        await fire(command, interaction, "3")

        assert "It was mirroring #9." in interaction.said

    async def test_the_same_board_again_says_nothing_about_replacing(self) -> None:
        command, interaction, _ = run_it(service=StubBoards(result=link(replaced=3)))

        await fire(command, interaction, "3")

        assert "was mirroring" not in interaction.said

    async def test_clearing_names_what_was_dropped(self) -> None:
        command, interaction, _ = run_it(service=StubBoards(result=link(number=0, replaced=9)))

        await fire(command, interaction, NONE_CHOSEN)

        assert "stopped mirroring board #9" in interaction.said

    async def test_clearing_when_there_was_nothing_to_clear(self) -> None:
        command, interaction, _ = run_it(service=StubBoards(result=link(number=0)))

        await fire(command, interaction, NONE_CHOSEN)

        assert "was not mirroring a board, and still is not" in interaction.said


class TestThePicker:
    def boards(self, how_many: int) -> tuple[ProjectListing, ...]:
        return tuple(ProjectListing(number=n, title=f"Board {n}") for n in range(1, how_many + 1))

    async def test_it_offers_the_owners_boards(self) -> None:
        suggest = _suggesting(StubBoards(listed=self.boards(2)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")

        assert [choice.name for choice in found[:2]] == ["#1 Board 1", "#2 Board 2"]

    async def test_the_clearing_choice_is_always_offered(self) -> None:
        suggest = _suggesting(StubBoards(listed=self.boards(2)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")

        assert found[-1].value == NONE_CHOSEN

    async def test_it_stays_inside_discords_cap_with_room_for_clearing(self) -> None:
        """Discord sends a longer list back as an error rather than truncating it, and the
        clearing entry has to survive the cut or it is the one thing nobody can pick."""
        suggest = _suggesting(StubBoards(listed=self.boards(40)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")

        assert len(found) == MOST_CHOICES
        assert found[-1].value == NONE_CHOSEN

    async def test_typing_narrows_by_title(self) -> None:
        suggest = _suggesting(
            StubBoards(
                listed=(
                    ProjectListing(number=1, title="Roadmap"),
                    ProjectListing(number=2, title="Bugs"),
                )
            )
        )

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "road")

        assert [choice.value for choice in found] == ["1", NONE_CHOSEN]

    async def test_typing_narrows_by_number(self) -> None:
        """An owner past twenty-five boards can only reach the rest by typing, and what somebody
        types into a field labelled by number is a number."""
        suggest = _suggesting(StubBoards(listed=self.boards(40)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "37")

        assert [choice.value for choice in found] == ["37", NONE_CHOSEN]

    async def test_it_reads_the_owner_typed_into_the_other_field(self) -> None:
        service = StubBoards(listed=self.boards(1))
        seen: list[str] = []

        async def remembering(guild_id: int, typed_owner: str) -> tuple[ProjectListing, ...]:
            seen.append(typed_owner)
            return service.listed

        service.choices_for = remembering  # type: ignore[method-assign]

        await _suggesting(service)(cast(discord.Interaction, FakeInteraction(owner="acme")), "")

        assert seen == ["acme"]

    async def test_an_owner_discord_has_not_sent_is_no_owner(self) -> None:
        """`namespace` carries only the options touched so far, so this is absent as readily as
        it is empty."""
        suggest = _suggesting(StubBoards(listed=self.boards(1)))

        assert await suggest(cast(discord.Interaction, FakeInteraction()), "") != []

    async def test_it_says_nothing_outside_a_server(self) -> None:
        suggest = _suggesting(StubBoards(listed=self.boards(1)))

        assert await suggest(cast(discord.Interaction, FakeInteraction(guild_id=None)), "") == []

    async def test_it_never_raises(self) -> None:
        """Discord shows an empty box whether the callback failed or the owner has no boards,
        and nobody can tell those apart, so raising makes an outage look like an empty account
        and takes the typed field down with it."""
        suggest = _suggesting(StubBoards(listing_error=RuntimeError("GitHub is down")))

        assert await suggest(cast(discord.Interaction, FakeInteraction()), "3") == []


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("3", 3), ("0", 0), (" 7 ", 7), ("bug", None), ("", None), ("-1", None), ("3.0", None)],
)
def test_what_a_typed_board_reads_as(typed: str, expected: int | None) -> None:
    """Three outcomes, not two. Zero is a board being cleared and None is something unreadable,
    and a signature that cannot tell them apart turns every typo into an unlinked board."""
    assert _wanted(typed) == expected
