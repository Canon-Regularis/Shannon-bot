"""`/link`, which is one command now and used to be two. Issue #144.

`/link` recorded a login somebody typed and asked GitHub only whether it existed; `/verify` asked
whose it was. What is left asks, and nobody types anything: the account written down is whatever
GitHub answered for whoever signed in, and following the link is the whole of it.

Two things here are worth more than the wording. The first is that naming a member hands out **no
link at all** — the URL is a bearer credential, so one issued for somebody else is that person's
identity in whoever's hands hold it, and the note in the channel carries none. The second is that
the note is the one message this bot sends that anybody but its caller can see, which is only
assertable at all since the fakes started recording it.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import discord
import pytest

from shannon.commands.link import CANNOT_POST, build_link_command
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import VerificationPurpose
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

ALICE = 555
BOB = 777


class FakeVerification:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.issued_for: list[int] = []
        self.purposes: list[VerificationPurpose] = []

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str:
        self.issued_for.append(discord_user_id)
        self.purposes.append(purpose)
        return "https://github.com/login/oauth/authorize?state=abc"


async def fire(
    command: SlashCommand, interaction: FakeInteraction, member: FakeMember | None = None
) -> None:
    """Run the command, with discord.py's typing answered once rather than at every call.

    Two things it cannot see through: the interaction and the member are stand-ins, so they are
    cast to the real ones, and `app_commands.Command` declares its parameters as `...`, which
    pyright reads as a definite arity rather than as anything goes. One suppression with its
    reason beside it leaves this file gated rather than sitting on the ratchet.
    """
    theirs = cast(discord.Interaction, interaction)
    them = cast(discord.Member, member) if member is not None else None
    await command.callback(theirs, them)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]


def run_it(*, verification: FakeVerification | None = None, who: FakeMember | None = None):
    verification = verification or FakeVerification()
    command = build_link_command(verification, default_gate())
    caller = who or developer()
    caller.id = ALICE
    return command, FakeInteraction(user=caller), verification


def somebody_else() -> FakeMember:
    other = FakeMember()
    other.id = BOB
    return other


class TestConnectingYourOwn:
    async def test_anybody_in_the_server_may(self) -> None:
        """No tier. GitHub decides which account this is, so a gate would only stop somebody
        proving who they are — and a developer is the lowest tier that holds a role at all."""
        command, interaction, verification = run_it()

        await fire(command, interaction)

        assert verification.issued_for == [ALICE]

    async def test_naming_yourself_is_the_same_thing(self) -> None:
        """On identity rather than on whether the argument was given, or somebody who typed their
        own name would be refused for something they are allowed to do."""
        me = developer()
        me.id = ALICE
        command, interaction, verification = run_it(who=me)

        await fire(command, interaction, me)

        assert verification.issued_for == [ALICE]

    async def test_the_link_is_issued_for_whoever_ran_it_and_nobody_else(self) -> None:
        """The invariant the whole command is built around. Whoever opens the URL is recorded as
        the person it was issued for, so an id taken from an argument would be an identity handed
        to whoever happened to be holding the link."""
        command, interaction, verification = run_it()

        await fire(command, interaction, somebody_else())

        assert BOB not in verification.issued_for

    async def test_it_asks_for_a_link_rather_than_an_unbinding(self) -> None:
        """The purpose is what the callback reads to decide whether the click finishes the job.
        Asking for the wrong one here would send somebody to `/unregister`."""
        command, interaction, verification = run_it()

        await fire(command, interaction)

        assert verification.purposes == [VerificationPurpose.LINK]

    async def test_it_does_not_tell_anybody_to_run_anything_again(self) -> None:
        """The whole of what issue #144 asked for. There is no second run, so a sentence saying
        there is would send somebody back to a command with nothing left to do."""
        command, interaction, _ = run_it()

        await fire(command, interaction)

        assert "again" not in interaction.reply

    async def test_run_outside_a_server_it_says_so(self) -> None:
        """`guild_only` keeps this out of a direct message, and the check stays anyway: the
        decorator is Discord's and this is what happens if it is ever removed or not enforced."""
        command, interaction, verification = run_it()
        interaction.guild_id = None

        await fire(command, interaction)

        assert interaction.reply == "Run this inside a server channel."
        assert verification.issued_for == []

    async def test_a_deployment_that_cannot_verify_anybody_says_so(self) -> None:
        """Said above the branch, because neither half of this works without it: asking somebody
        to go and try would be as useless as handing out a link that goes nowhere."""
        command, interaction, verification = run_it(verification=FakeVerification(configured=False))

        await fire(command, interaction)

        assert "cannot check who you are on GitHub" in interaction.reply
        assert verification.issued_for == []


class TestAskingSomebodyElse:
    async def test_it_hands_out_no_link_at_all(self) -> None:
        """The security test, and the direct replacement for the promise `/verify` used to carry
        that no link would ever be issued for anybody but the caller.

        A link issued for another member and shown to whoever asked is that member's identity:
        the URL is a bearer credential, and the row records the person it was issued for rather
        than the person who opens it.
        """
        command, interaction, verification = run_it(who=project_manager())

        await fire(command, interaction, somebody_else())

        assert verification.issued_for == []

    async def test_the_note_names_them(self) -> None:
        command, interaction, _ = run_it(who=project_manager())

        await fire(command, interaction, somebody_else())

        assert f"<@{BOB}>" in interaction.reply
        assert "/link" in interaction.reply

    async def test_the_note_is_the_one_message_anybody_else_can_see(self) -> None:
        """Every other reply in this project is private, and this one cannot be: it is addressed
        to somebody who did not run the command and is not watching for a reply."""
        command, interaction, _ = run_it(who=project_manager())

        await fire(command, interaction, somebody_else())

        assert interaction.ephemerally == [False]

    async def test_a_project_manager_may(self) -> None:
        command, interaction, _ = run_it(who=project_manager())

        await fire(command, interaction, somebody_else())

        assert interaction.ephemerally == [False]

    async def test_an_administrator_may(self) -> None:
        command, interaction, _ = run_it(who=administrator())

        await fire(command, interaction, somebody_else())

        assert interaction.ephemerally == [False]

    async def test_somebody_without_the_tier_is_refused(self) -> None:
        """Pinging a member in public on anybody's say-so is a spam tool, which is the half of
        this command that keeps a gate."""
        command, interaction, verification = run_it(who=developer())

        await fire(command, interaction, somebody_else())

        assert "You need one of these roles" in interaction.reply
        assert verification.issued_for == []

    async def test_a_refusal_is_private_like_every_other_one(self) -> None:
        command, interaction, _ = run_it(who=developer())

        await fire(command, interaction, somebody_else())

        assert interaction.ephemerally == [True]

    async def test_a_channel_it_cannot_post_in_says_which_permission(self) -> None:
        """An ephemeral reply always lands and a public one needs Send Messages, so this is the
        one refusal this command answers for. Left to the tree's handler it would read as
        "Something went wrong here" and the person owed an explanation would get none."""
        command, interaction, _ = run_it(who=project_manager())
        landed = interaction.response.send_message

        async def only_in_private(
            content: str | None = None, *, ephemeral: bool = False, **rest: object
        ) -> None:
            """A channel this bot may not post in, which is not a channel it may not answer in.

            The asymmetry is the whole of why there is a fallback at all: an ephemeral response
            is part of the interaction and always lands, and only the public note needs Send
            Messages. A stand-in that refused both would have the explanation fail too.
            """
            if not ephemeral:
                raise discord.HTTPException(MagicMock(status=403), "no")
            await landed(content, ephemeral=ephemeral, **rest)

        interaction.response.send_message = only_in_private

        await fire(command, interaction, somebody_else())

        assert interaction.reply == CANNOT_POST
        assert interaction.ephemerally == [True]
