"""`/authorise_board`: granting this bot a board credential of your own, and taking it back.

Issue #170. The command that replaced one shared classic token belonging to one human account.
What is pinned here is mostly about what it will NOT do: issue a link for anybody but the runner,
ask GitHub for anything when nothing could store the answer, or claim to have withdrawn something
it never held.
"""

from __future__ import annotations

import inspect
from typing import cast

import discord
import pytest

from shannon.commands.authorise_board import (
    NOT_CONFIGURED,
    NOTHING_TO_WITHDRAW,
    build_authorise_board_command,
)
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import VerificationPurpose
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import administrator, developer

pytestmark = pytest.mark.unit

ALICE = 555


class FakeVerification:
    def __init__(self, *, can: bool = True) -> None:
        self.can_authorise_a_board = can
        self.issued_for: list[int] = []
        self.purposes: list[VerificationPurpose] = []

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str:
        self.issued_for.append(discord_user_id)
        self.purposes.append(purpose)
        return "https://github.com/login/oauth/authorize?scope=project&state=abc"


class FakeAuthorisations:
    def __init__(self, *, held: bool = True) -> None:
        self.held = held
        self.forgotten: list[tuple[int, int]] = []

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool:
        self.forgotten.append((guild_id, discord_user_id))
        return self.held


async def fire(
    command: SlashCommand, interaction: FakeInteraction, *, withdraw: bool = False
) -> None:
    """Run the command, with discord.py's typing answered once rather than at every call."""
    theirs = cast(discord.Interaction, interaction)
    await command.callback(theirs, withdraw)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]


def run_it(
    *, can: bool = True, held: bool = True, who=None
) -> tuple[SlashCommand, FakeInteraction, FakeVerification, FakeAuthorisations]:
    verification = FakeVerification(can=can)
    authorisations = FakeAuthorisations(held=held)
    command = build_authorise_board_command(verification, authorisations)
    return command, FakeInteraction(user=who or developer()), verification, authorisations


class TestGrantingOne:
    async def test_anybody_in_the_server_may(self) -> None:
        """Ungated, like `/mentions`. Granting a credential of your own is yours to decide, and a
        role gate could only stop somebody volunteering one."""
        command, interaction, verification, _ = run_it()

        await fire(command, interaction)

        assert verification.issued_for == [interaction.user.id]

    async def test_the_link_is_for_a_board_and_not_an_identity(self) -> None:
        """The purpose is what picks the application and the scope, so getting it wrong here would
        hand out a link against the App, which cannot read a board at all."""
        command, interaction, verification, _ = run_it()

        await fire(command, interaction)

        assert verification.purposes == [VerificationPurpose.BOARD]

    async def test_the_reply_carries_the_link_and_says_what_it_is_for(self) -> None:
        """People are reasonably wary of an OAuth screen asking for project access, and "full
        control of projects" is what GitHub shows them. The reply says what it is used for before
        they get there."""
        command, interaction, _, _ = run_it()

        await fire(command, interaction)

        said = interaction.said
        assert "authorize?scope=project" in said
        assert "moved as you" in said

    async def test_it_is_issued_for_whoever_ran_it_and_nobody_else(self) -> None:
        """The same invariant `/link` keeps. The URL is a bearer credential: whoever opens it is
        recorded as the person it was issued for."""
        command, interaction, verification, _ = run_it(who=administrator())

        await fire(command, interaction)

        assert verification.issued_for == [interaction.user.id]

    async def test_it_takes_no_argument_that_could_name_somebody_else(self) -> None:
        """`/link` has a member argument so it can REFUSE to issue for them. Here there is no half
        that could, so the argument does not exist - a parameter naming somebody else would be a
        way to collect their credential."""
        command, _, _, _ = run_it()

        taken = set(inspect.signature(command.callback).parameters)
        assert taken == {"interaction", "withdraw"}

    async def test_a_deployment_with_no_oauth_app_says_so_and_asks_github_nothing(self) -> None:
        """Fail closed, and before the round trip: sending somebody to GitHub to grant something
        that cannot then be stored would leave a real authorisation on their account with nothing
        here using it."""
        command, interaction, verification, _ = run_it(can=False)

        await fire(command, interaction)

        assert interaction.said.endswith(NOT_CONFIGURED)
        assert verification.issued_for == []

    async def test_run_outside_a_server_it_says_so(self) -> None:
        """The decorator is Discord's; this is what happens if it is ever removed or not
        enforced."""
        command, interaction, verification, _ = run_it()
        interaction.guild_id = None

        await fire(command, interaction)

        assert verification.issued_for == []


class TestWithdrawingOne:
    async def test_it_forgets_what_was_held(self) -> None:
        command, interaction, _, authorisations = run_it()

        await fire(command, interaction, withdraw=True)

        assert authorisations.forgotten == [(interaction.guild_id, interaction.user.id)]

    async def test_it_says_that_forgetting_is_not_revoking(self) -> None:
        """The honest half. Dropping this copy does not withdraw the grant on GitHub's side, and
        only the person who granted it can do that - so the reply says where."""
        command, interaction, _, _ = run_it()

        await fire(command, interaction, withdraw=True)

        assert "not the same as revoking" in interaction.said
        assert "Authorized OAuth Apps" in interaction.said

    async def test_withdrawing_what_was_never_held_says_that_instead(self) -> None:
        """A repeat is not a failure, and "it is gone" would be a claim about something that was
        never there."""
        command, interaction, _, _ = run_it(held=False)

        await fire(command, interaction, withdraw=True)

        assert interaction.said.endswith(NOTHING_TO_WITHDRAW)

    async def test_withdrawing_asks_github_nothing(self) -> None:
        """One delete, and nothing that talks to GitHub - which is why it does not defer."""
        command, interaction, verification, _ = run_it()

        await fire(command, interaction, withdraw=True)

        assert verification.issued_for == []

    async def test_withdrawing_outside_a_server_forgets_nothing(self) -> None:
        command, interaction, _, authorisations = run_it()
        interaction.guild_id = None

        await fire(command, interaction, withdraw=True)

        assert authorisations.forgotten == []
