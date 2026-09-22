"""`/verify`, and the two runs it takes.

The command that closes the hole `/link` leaves open: a link is a login somebody typed and nobody
checked, so an admin can bind a member to an account that is not theirs, and until now the only
way to fix one was for an admin to type it again. Here GitHub says who followed the link, and the
only account anybody can bind is the one they have just signed into.

Nobody types a login, which is what makes it safe to leave ungated. What is worth pinning is that
the login written down is the one GitHub answered with, and never anything the caller supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import discord
import pytest

from shannon.commands.verify import build_verify_command
from shannon.db.stores.identities import ProvedAccount
from shannon.discord_bot.slash import SlashCommand
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import developer

pytestmark = pytest.mark.unit

ALICE = 555
PROVED = ProvedAccount(
    login="octocat", github_user_id=583231, verified_at=datetime(2026, 9, 22, tzinfo=UTC)
)


class FakeVerification:
    def __init__(self, *, configured: bool = True, proved: ProvedAccount | None = PROVED) -> None:
        self.configured = configured
        self.proved = proved
        self.links_handed_out = 0

    async def proved_just_now(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        return self.proved

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str:
        self.links_handed_out += 1
        return "https://github.com/login/oauth/authorize?state=abc"


class FakeLinking:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, int]] = []

    async def bind(
        self, *, guild_id: int, discord_user_id: int, login: str, github_user_id: int
    ) -> str:
        self.calls.append((guild_id, discord_user_id, login, github_user_id))
        return login.lower()


async def fire(command: SlashCommand, interaction: FakeInteraction) -> None:
    """Run the command, with discord.py's typing answered once rather than at every call.

    Two things it cannot see through. The interaction is a stand-in, so it is cast to the real
    one; and `app_commands.Command` declares its parameters as `...`, which pyright reads as a
    definite arity rather than as anything goes, so a callback taking only an interaction looks
    to it like a call missing an argument. Every other command test in this package answers both
    by sitting on the ratchet in `pyproject.toml`. One suppression with its reason beside it is
    the smaller debt, and it leaves this file gated.
    """
    theirs = cast(discord.Interaction, interaction)
    await command.callback(theirs)  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]


def run_it(*, verification: FakeVerification | None = None):
    verification = verification or FakeVerification()
    service = FakeLinking()
    command = build_verify_command(service, verification)
    # A developer, deliberately: the lowest tier that holds a role at all. Nothing here reads it,
    # and a test written with an administrator would pass whether or not that were true.
    who = developer()
    who.id = ALICE
    return command, FakeInteraction(user=who), verification, service


class TestWhoMayRunIt:
    async def test_anybody_in_the_server_may(self) -> None:
        """No tier, on purpose. A gate here would only stop somebody proving who they are, and
        the account they can bind is the one they have just signed into."""
        command, interaction, _, service = run_it()

        await fire(command, interaction)

        assert service.calls == [(1, ALICE, "octocat", 583231)]

    async def test_run_outside_a_server_it_says_so(self) -> None:
        """`guild_only` keeps this out of a direct message, and the check stays anyway: the
        decorator is Discord's and this is what happens if it is ever removed or not enforced."""
        command, interaction, _, service = run_it()
        interaction.guild_id = None

        await fire(command, interaction)

        assert interaction.reply == "Run this inside a server channel."
        assert service.calls == []

    async def test_a_deployment_that_cannot_verify_anybody_says_so(self) -> None:
        """Unlike `/unregister`, this is said to anybody who asks, because there is no tier above
        it to keep the deployment's shape from. Somebody told to run /verify deserves to know why
        it cannot work rather than be left clicking a link that goes nowhere."""
        command, interaction, _, service = run_it(verification=FakeVerification(configured=False))

        await fire(command, interaction)

        assert "cannot check who you are on GitHub" in interaction.reply
        assert service.calls == []


class TestTheFirstRun:
    async def test_somebody_who_has_not_proved_anything_gets_a_link(self) -> None:
        verification = FakeVerification(proved=None)
        command, interaction, _, _ = run_it(verification=verification)

        await fire(command, interaction)

        assert "authorize" in interaction.reply
        assert verification.links_handed_out == 1

    async def test_it_says_to_run_the_command_again(self) -> None:
        """The handshake only works if the person knows there is a second half to it."""
        command, interaction, _, _ = run_it(verification=FakeVerification(proved=None))

        await fire(command, interaction)

        assert "run /verify again" in interaction.reply

    async def test_nothing_is_linked_on_that_run(self) -> None:
        command, interaction, _, service = run_it(verification=FakeVerification(proved=None))

        await fire(command, interaction)

        assert service.calls == []


class TestTheSecondRun:
    async def test_the_account_github_named_is_the_one_written_down(self) -> None:
        """Both halves of it. The id is what a stored link is held against later, so a command
        that recorded the name alone would leave the row no better off than `/link` does."""
        command, interaction, _, service = run_it()

        await fire(command, interaction)

        assert service.calls == [(1, ALICE, "octocat", 583231)]

    async def test_it_is_bound_to_whoever_ran_it_and_nobody_else(self) -> None:
        """There is no member argument and there is not going to be one: the proof is about the
        person at the keyboard, so binding it to anybody else would be a claim again."""
        command, interaction, _, service = run_it()

        await fire(command, interaction)

        assert [discord_user_id for _, discord_user_id, _, _ in service.calls] == [ALICE]

    async def test_the_reply_names_the_account(self) -> None:
        """Somebody who typed their login into `/link` years ago and is now told a different name
        has just found out their link was wrong, and that is the whole value of the sentence."""
        command, interaction, _, _ = run_it()

        assert "octocat" in (await _said(command, interaction))

    async def test_no_link_is_handed_out_when_one_has_already_been_followed(self) -> None:
        command, interaction, verification, _ = run_it()

        await fire(command, interaction)

        assert verification.links_handed_out == 0


async def _said(command, interaction) -> str:
    await fire(command, interaction)
    return interaction.reply
