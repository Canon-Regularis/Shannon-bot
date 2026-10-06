"""`/board`, against stubs.

Issue #201. Two commands became one with five halves, and linking a board became one click. Most of
what is pinned here is what the merge must NOT have lost - the parsing of what somebody typed, the
link only ever issued for its runner, the honest half of withdrawing - and the tiers, which a
factory gated on some of its halves has to hold for itself because `_permissions.UNGATED` cannot.

A choice is a SUGGESTION. discord.py says so, and Discord will send whatever somebody typed instead,
so the number that arrives may be prose, a digit from another script, or longer than any column
can hold, and has to be turned away with a sentence.
"""

from __future__ import annotations

import inspect
from collections.abc import Collection
from typing import Any, cast

import discord
import pytest
from discord import app_commands

from shannon.commands._permissions import BOARD_ROLES, REGISTER_ROLES
from shannon.commands.board import (
    MOST_CHOICES,
    MOST_LABEL,
    NOT_CONFIGURED,
    NOTHING_TO_WITHDRAW,
    _label_for,
    _suggesting,
    _wanted,
    build_board_command,
)
from shannon.discord_bot.responses import OWED, REFUSED, SUCCEEDED
from shannon.discord_bot.roles import CommandRole
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose
from shannon.domain.errors import BoardNotAuthorisedError, NotRegisteredError
from shannon.github.errors import GitHubUnavailableError
from shannon.github.projects import ProjectListing
from shannon.services.boards import (
    BoardLink,
    BoardStanding,
    BoardTakenError,
    BoardUnlinked,
    BoardUnreadableError,
    said,
)
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

GUILD = 1
URL = "https://shannon.example.com/oauth/start?state=abc"


def link(*, number: int = 3, replaced: int | None = None) -> BoardLink:
    return BoardLink(
        repo_name="acme/widget", owner="acme", number=number, title="Roadmap", replaced=replaced
    )


def standing(**changes: Any) -> BoardStanding:
    """A server mirroring acme's board #3, read as member 42, asked by somebody unauthorised."""
    fields: dict[str, Any] = {
        "repo_name": "acme/widget",
        "number": 3,
        "owner": "acme",
        "title": "Roadmap",
        "linked_by": 42,
        "held": True,
        "yours": None,
    }
    fields.update(changes)
    return BoardStanding(**fields)


class StubBoards:
    """`LinksBoards`, answering whatever a test says and recording what it was asked."""

    def __init__(
        self,
        *,
        result: BoardLink | None = None,
        error: Exception | None = None,
        listed: tuple[ProjectListing, ...] = (),
        listing_error: Exception | None = None,
        unlinked: BoardUnlinked | None = None,
        standing_is: BoardStanding | None = None,
    ) -> None:
        self.result = result or link()
        self.error = error
        self.listed = listed
        self.listing_error = listing_error
        self.unlinked = unlinked or BoardUnlinked(
            repo_name="acme/widget", replaced=None, forgot=None
        )
        self.standing_is = standing_is or standing()
        self.calls: list[tuple[int, int, str]] = []
        self.acted: list[int] = []
        self.unassigned: list[int] = []
        self.asked: list[tuple[int, int]] = []
        self.chosen_by: list[tuple[int, str, int]] = []

    async def assign(
        self, *, guild_id: int, project_number: int, typed_owner: str, acting: int
    ) -> BoardLink:
        self.calls.append((guild_id, project_number, typed_owner))
        self.acted.append(acting)
        if self.error is not None:
            raise self.error
        return self.result

    async def unassign(self, *, guild_id: int) -> BoardUnlinked:
        self.unassigned.append(guild_id)
        if self.error is not None:
            raise self.error
        return self.unlinked

    async def standing(self, *, guild_id: int, asking: int) -> BoardStanding:
        self.asked.append((guild_id, asking))
        if self.error is not None:
            raise self.error
        return self.standing_is

    async def choices_for(
        self, guild_id: int, typed_owner: str, *, acting: int
    ) -> tuple[ProjectListing, ...]:
        self.chosen_by.append((guild_id, typed_owner, acting))
        if self.listing_error is not None:
            raise self.listing_error
        return self.listed


class FakeVerification:
    """`AuthorisesBoards`: hands out one URL, and remembers who for and what it carried."""

    def __init__(self, *, can: bool = True) -> None:
        self.can_authorise_a_board = can
        self.issued_for: list[int] = []
        self.purposes: list[VerificationPurpose] = []
        self.boards: list[ChosenBoard | None] = []
        self.tiers: list[frozenset[CommandRole]] = []

    async def link_for(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        purpose: VerificationPurpose,
        board: ChosenBoard | None = None,
        tier: Collection[CommandRole],
    ) -> str:
        self.issued_for.append(discord_user_id)
        self.purposes.append(purpose)
        self.boards.append(board)
        self.tiers.append(frozenset(tier))
        return URL


class FakeAuthorisations:
    def __init__(self, *, held: bool = True) -> None:
        self.held = held
        self.forgotten: list[tuple[int, int]] = []

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool:
        self.forgotten.append((guild_id, discord_user_id))
        return self.held


class Built:
    """The group and every stand-in behind it, so a test reads the one it is about by name."""

    def __init__(
        self,
        *,
        boards: StubBoards | None = None,
        can: bool = True,
        held: bool = True,
    ) -> None:
        self.boards = boards or StubBoards()
        self.verification = FakeVerification(can=can)
        self.authorisations = FakeAuthorisations(held=held)
        self.group = build_board_command(
            self.boards, self.verification, self.authorisations, default_gate()
        )

    def sub(self, name: str) -> app_commands.Command[Any, ..., Any]:
        found = self.group.get_command(name)
        assert isinstance(found, app_commands.Command), f"/board has no {name}"
        return found


async def fire(
    command: app_commands.Command[Any, ..., Any], interaction: FakeInteraction, *args: str
) -> None:
    """Run one half, with discord.py's typing answered once rather than at every call.

    Two things it cannot see through: the interaction is a stand-in, so it is cast to the real
    one, and `app_commands.Command` declares its parameters as `...`, which pyright reads as a
    definite arity rather than as anything goes. One suppression with its reason beside it.
    """
    theirs = cast(discord.Interaction, interaction)
    await command.callback(theirs, *args)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]


def nobody() -> FakeMember:
    """A member holding no role this bot knows about."""
    return FakeMember()


class TestTheCommandsShape:
    def test_it_is_one_command_with_five_halves(self) -> None:
        group = Built().group

        assert group.name == "board"
        assert {command.name for command in group.commands} == {
            "link",
            "unlink",
            "authorise",
            "withdraw",
            "show",
        }

    def test_it_runs_in_a_server_and_nowhere_else(self) -> None:
        """Discord reads this from the top-level command alone and ignores it on a subcommand,
        so it is on the group - and through the decorator, which sends the guild as the context
        where the constructor's own argument would send none."""
        group = Built().group
        contexts = group.allowed_contexts

        assert group.guild_only is True
        assert contexts is not None
        assert (contexts.guild, contexts.dm_channel, contexts.private_channel) == (
            True,
            False,
            False,
        )

    @pytest.mark.parametrize(
        ("name", "taken"),
        [
            ("link", {"interaction", "board", "owner"}),
            ("unlink", {"interaction"}),
            ("authorise", {"interaction"}),
            ("withdraw", {"interaction"}),
            ("show", {"interaction"}),
        ],
    )
    def test_no_half_takes_an_argument_that_could_name_somebody_else(
        self, name: str, taken: set[str]
    ) -> None:
        """The invariant `/link` keeps. Every link is issued for whoever ran the command: a
        credential is granted by the person it belongs to, from their own command. Since #201's
        review Discord refuses anybody else who follows a link, and the rule stands regardless."""
        command = Built().sub(name)

        assert set(inspect.signature(command.callback).parameters) == taken


class TestWhoMayRunEachHalf:
    @pytest.mark.parametrize("who", [project_manager, administrator])
    @pytest.mark.parametrize(("name", "args"), [("link", ("3",)), ("unlink", ())])
    async def test_the_tiers_that_speak_for_the_server_may_link_and_unlink(
        self, who: Any, name: str, args: tuple[str, ...]
    ) -> None:
        built = Built()
        interaction = FakeInteraction(user=who())

        await fire(built.sub(name), interaction, *args)

        assert built.boards.calls or built.boards.unassigned

    @pytest.mark.parametrize(("name", "args"), [("link", ("3",)), ("unlink", ())])
    async def test_anybody_else_is_refused_before_anything_is_written(
        self, name: str, args: tuple[str, ...]
    ) -> None:
        built = Built()
        interaction = FakeInteraction(user=developer())

        await fire(built.sub(name), interaction, *args)

        assert "You need one of these roles to use /board " in interaction.reply
        assert (built.boards.calls, built.boards.unassigned) == ([], [])

    @pytest.mark.parametrize("who", [project_manager, administrator])
    @pytest.mark.parametrize("name", ["authorise", "show"])
    async def test_the_tiers_whose_authorisation_is_used_may_authorise_and_ask(
        self, who: Any, name: str
    ) -> None:
        built = Built()
        interaction = FakeInteraction(user=who())

        await fire(built.sub(name), interaction)

        assert built.verification.issued_for or built.boards.asked

    @pytest.mark.parametrize("name", ["authorise", "show"])
    async def test_a_tier_whose_authorisation_nothing_uses_may_not(self, name: str) -> None:
        """A developer moves no card and links no board, so a credential from one is a credential
        nothing would ever use - which is the worst kind to be holding."""
        built = Built()
        interaction = FakeInteraction(user=developer())

        await fire(built.sub(name), interaction)

        assert "You need one of these roles" in interaction.reply
        assert (built.verification.issued_for, built.boards.asked) == ([], [])

    @pytest.mark.parametrize("who", [nobody, developer, project_manager, administrator])
    async def test_anybody_may_withdraw_their_own(self, who: Any) -> None:
        """Ungated on purpose. It deletes a credential that belongs to whoever runs it, and that
        must not depend on a role they may since have lost - a project manager who authorised
        and was then demoted still owns what they granted."""
        built = Built()
        interaction = FakeInteraction(user=who())

        await fire(built.sub("withdraw"), interaction)

        assert built.authorisations.forgotten == [(GUILD, interaction.user.id)]

    @pytest.mark.parametrize(
        ("name", "args"),
        [("link", ("3",)), ("unlink", ()), ("authorise", ()), ("withdraw", ()), ("show", ())],
    )
    async def test_outside_a_server_every_half_says_so_and_does_nothing(
        self, name: str, args: tuple[str, ...]
    ) -> None:
        """The decorator is Discord's; this is what happens if it is ever not enforced."""
        built = Built()
        interaction = FakeInteraction()
        interaction.guild_id = None

        await fire(built.sub(name), interaction, *args)

        assert interaction.said == "Run this inside a server channel."
        assert (built.boards.calls, built.boards.unassigned, built.boards.asked) == ([], [], [])
        assert (built.verification.issued_for, built.authorisations.forgotten) == ([], [])


class TestWhatArrivesInTheBoardField:
    """A choice is a suggestion. Whatever Discord sends has to survive being read."""

    async def test_a_number_that_was_offered(self) -> None:
        built = Built()

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "3")

        assert built.boards.calls == [(GUILD, 3, "")]

    @pytest.mark.parametrize(
        "typed",
        ["Roadmap", "", "  ", "3.0", "-1", "0", "#3abc", "None", "²", "12345678901", "9" * 5000],
    )
    async def test_what_is_not_a_board_is_refused_with_a_way_in(self, typed: str) -> None:
        """Zero, which used to mean "stop mirroring", is not a board any more: unlinking is its own
        half, and a typo one character from unlinking somebody's board was the bug this replaced.
        The digit from another script and the eleven digits used to get past the parser and crash
        on the way to an integer or to Postgres."""
        built = Built()
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, typed)

        assert "is not a board" in interaction.said
        assert "paste the board's URL" in interaction.said, "it offered no way in"
        # The number AFTER /projects/, not off the end: a board opens on /projects/6/views/1,
        # and the number off the end of that is a view, which can name somebody else's board.
        assert "after /projects/" in interaction.said
        assert built.boards.calls == []

    async def test_the_pickers_own_label_is_accepted(self) -> None:
        """Discord sends a choice's VALUE when the entry is committed and the raw text when it is
        typed or a highlighted suggestion is let fall through - so picking "#6 Shannon Bot" off
        the list was once answered with "that is not a board", told to somebody who just had."""
        built = Built()

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "#6 Shannon Bot")

        assert built.boards.calls == [(GUILD, 6, "")]

    async def test_a_pasted_url_carries_its_owner(self) -> None:
        built = Built()

        await fire(
            built.sub("link"),
            FakeInteraction(user=administrator()),
            "https://github.com/orgs/acme/projects/6",
        )

        assert built.boards.calls == [(GUILD, 6, "acme")]

    async def test_a_typed_owner_is_passed_through(self) -> None:
        built = Built()

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "3", "acme")

        assert built.boards.calls == [(GUILD, 3, "acme")]

    async def test_a_typed_owner_beats_a_pasted_one(self) -> None:
        """Somebody who filled in both meant the one they typed."""
        built = Built()

        await fire(
            built.sub("link"),
            FakeInteraction(user=administrator()),
            "https://github.com/users/mona/projects/6",
            "acme",
        )

        assert built.boards.calls == [(GUILD, 6, "acme")]

    async def test_an_enterprise_managed_users_login_is_an_account(self) -> None:
        """Their logins are `handle_shortcode`, and refusing the underscore refused every
        board one of them owns."""
        built = Built()

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "3", "mona-cat_octo")

        assert built.boards.calls == [(GUILD, 3, "mona-cat_octo")]

    @pytest.mark.parametrize("owner", ["a b", "-acme", "acme/widget", "ácme", "a" * 40])
    async def test_an_owner_that_is_not_a_github_account_is_refused(self, owner: str) -> None:
        """It is stored, and it becomes part of a request path. Whether the account exists is
        GitHub's to say; whether the text could be one is this command's."""
        built = Built()
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3", owner)

        assert "is not a GitHub account name" in interaction.said
        assert built.boards.calls == []


class TestLinkingWithNoAuthorisationYet:
    """One click. The link handed out remembers the board, and following it links it."""

    async def test_it_hands_out_a_link_that_carries_the_board(self) -> None:
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert built.verification.purposes == [VerificationPurpose.BOARD]
        assert built.verification.boards == [ChosenBoard(number=3, owner="")]

    async def test_it_is_issued_for_whoever_ran_it(self) -> None:
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("link"), interaction, "3")

        assert built.verification.issued_for == [interaction.user.id]

    async def test_the_reply_says_the_click_is_the_whole_of_it(self) -> None:
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3", "acme")

        assert interaction.mark == OWED
        assert URL in interaction.said
        assert "acme's board #3 is linked when you do" in interaction.said
        assert "nothing else to run" in interaction.said

    async def test_it_says_what_the_grant_is_for_and_how_to_take_it_back(self) -> None:
        """People are reasonably wary of an OAuth screen asking for project access, and "full
        control of projects" is what GitHub shows them."""
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert "moves as you" in interaction.said
        assert "the board is read with it" in interaction.said, "linking it is what it is for"
        assert "/board withdraw" in interaction.said
        # GitHub shows an owner the Grant button on a first sign-in only, so the line also says
        # where anybody can ask for approval afterwards.
        assert "Grant button" in interaction.said, "an organisation's board would fail unexplained"
        assert "under Applications in their own GitHub settings" in interaction.said

    async def test_a_deployment_that_cannot_authorise_says_so_and_issues_nothing(self) -> None:
        """Fail closed, and before the round trip: sending somebody to GitHub to grant something
        that cannot then be stored would leave a real authorisation on their account with nothing
        here using it."""
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")), can=False)
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert interaction.said == NOT_CONFIGURED
        assert built.verification.issued_for == []

    def test_not_being_set_up_names_everything_that_could_be_missing(self) -> None:
        """It says no more than that the setup is incomplete. An admin who registered the app
        and only lacks the key was told they had registered nothing."""
        for setting in (
            "SHANNON_GITHUB_BOARD_CLIENT_ID",
            "SHANNON_BOARD_CREDENTIAL_KEY",
            "SHANNON_DISCORD_CLIENT_ID",
            "SHANNON_DISCORD_CLIENT_SECRET",
            "SHANNON_PUBLIC_BASE_URL",
        ):
            assert setting in NOT_CONFIGURED
        assert "no GitHub application registered" not in NOT_CONFIGURED


class TestLinkingABoardThatWillNotOpen:
    async def test_the_reason_comes_first_and_a_link_follows(self) -> None:
        """A wrong number is the likelier cause, and signing in again will not fix that one - but
        a grant revoked on GitHub and an organisation that has not approved this app are both
        fixed by signing in, so the link is there for those."""
        built = Built(boards=StubBoards(error=BoardUnreadableError("acme has no board numbered 3")))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert interaction.said.startswith("acme has no board numbered 3")
        assert URL in interaction.said
        assert built.verification.boards == [ChosenBoard(number=3, owner="")]
        # Signing in again cannot fix an organisation that has not approved the app - GitHub
        # completes a second sign-in without showing anything - so that is said apart.
        assert "Third-party access" in interaction.said

    async def test_with_nobody_to_send_anywhere_the_reason_is_the_answer(self) -> None:
        built = Built(
            boards=StubBoards(error=BoardUnreadableError("acme has no board numbered 3")),
            can=False,
        )
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert interaction.mark == REFUSED
        assert interaction.said == "acme has no board numbered 3"
        assert built.verification.issued_for == []


class TestWhenLinkingIsRefused:
    async def test_a_server_with_no_repository(self) -> None:
        """Through the reply table like every other command's: the same refusal reaching a person
        from two commands should read the same way from both."""
        built = Built(
            boards=StubBoards(
                error=NotRegisteredError("This server has no repository yet. Run /register first.")
            )
        )
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert "/register" in interaction.said

    async def test_a_board_another_server_already_mirrors(self) -> None:
        built = Built(boards=StubBoards(error=BoardTakenError("Another server already mirrors it")))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert interaction.said == "Another server already mirrors it"
        assert built.verification.issued_for == [], "signing in again cannot fix a taken board"


class TestWhatLinkingSaysBack:
    async def test_it_says_what_said_says(self) -> None:
        """One sentence for Discord and for the browser page after a one-click link, so the two
        cannot drift."""
        built = Built(boards=StubBoards(result=link(replaced=9)))
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("link"), interaction, "3")

        assert interaction.mark == SUCCEEDED
        assert interaction.said == said(link(replaced=9))


class TestUnlinking:
    async def test_it_unlinks_this_servers_board(self) -> None:
        built = Built()

        await fire(built.sub("unlink"), FakeInteraction(user=administrator()))

        assert built.boards.unassigned == [GUILD]

    async def test_it_defers_before_it_does_anything(self) -> None:
        """Unlinking forgets every card pairing in one bulk update and then deletes a credential
        in a second transaction, which a large repository or a lock wait can stretch past the
        three seconds Discord allows an answer."""
        built = Built()
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("unlink"), interaction)

        assert interaction.followup.messages, "it answered without deferring first"

    async def test_a_server_mirroring_nothing_is_told_so(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("unlink"), interaction)

        assert interaction.said == "acme/widget was not mirroring a board, and still is not."

    async def test_it_names_what_was_dropped(self) -> None:
        built = Built(
            boards=StubBoards(
                unlinked=BoardUnlinked(repo_name="acme/widget", replaced=9, forgot=None)
            )
        )
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("unlink"), interaction)

        assert "stopped mirroring board #9" in interaction.said
        assert "authorisation" not in interaction.said, "nothing was forgotten, so nobody is named"

    async def test_it_names_whose_authorisation_went_and_that_only_they_can_revoke_it(
        self,
    ) -> None:
        """The person who ran this may well not be them, and they are the only one who can also
        revoke it on GitHub. Named by their Discord account, never the GitHub login it was granted
        as, which is theirs."""
        built = Built(
            boards=StubBoards(
                unlinked=BoardUnlinked(repo_name="acme/widget", replaced=9, forgot=42)
            )
        )
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("unlink"), interaction)

        assert "<@42>'s GitHub authorisation was forgotten" in interaction.said
        assert "not revoking it" in interaction.said
        assert "Authorized OAuth Apps" in interaction.said

    async def test_a_server_with_no_repository(self) -> None:
        built = Built(
            boards=StubBoards(
                error=NotRegisteredError("This server has no repository yet. Run /register first.")
            )
        )
        interaction = FakeInteraction(user=administrator())

        await fire(built.sub("unlink"), interaction)

        assert "/register" in interaction.said


class TestTheTierRidesWithTheLink:
    """Found reviewing #201. A link is followed up to ten minutes after the command checked the
    role, and following it asks Discord again for the tier the command was gated on - so each half
    that hands one out has to say which."""

    async def test_linking_carries_the_tier_that_links(self) -> None:
        built = Built(boards=StubBoards(error=BoardNotAuthorisedError("none")))

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "3")

        assert built.verification.tiers == [REGISTER_ROLES]

    async def test_a_board_that_will_not_open_carries_it_too(self) -> None:
        built = Built(boards=StubBoards(error=BoardUnreadableError("acme has no board numbered 3")))

        await fire(built.sub("link"), FakeInteraction(user=administrator()), "3")

        assert built.verification.tiers == [REGISTER_ROLES]

    async def test_authorising_carries_the_tier_that_authorises(self) -> None:
        built = Built()

        await fire(built.sub("authorise"), FakeInteraction(user=project_manager()))

        assert built.verification.tiers == [BOARD_ROLES]


class TestAuthorisingWithoutLinking:
    async def test_the_link_is_for_a_board_and_carries_none(self) -> None:
        """The purpose picks the application and the scope, so a wrong one would hand out a link
        against the App, which cannot read a board at all. And no board rides on it: this half
        changes nothing about what the server mirrors."""
        built = Built()
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("authorise"), interaction)

        assert built.verification.purposes == [VerificationPurpose.BOARD]
        assert built.verification.boards == [None]
        assert built.verification.issued_for == [interaction.user.id]

    async def test_the_reply_carries_the_link_and_says_what_it_is_for(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("authorise"), interaction)

        assert interaction.mark == OWED
        assert URL in interaction.said
        assert "moves as you" in interaction.said
        # Only the member who LINKS a board has it read with their authorisation.
        assert "the board is read with it" not in interaction.said

    async def test_a_deployment_that_cannot_authorise_says_so_and_issues_nothing(self) -> None:
        built = Built(can=False)
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("authorise"), interaction)

        assert interaction.said == NOT_CONFIGURED
        assert built.verification.issued_for == []


class TestWithdrawing:
    async def test_it_forgets_what_was_held(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=developer())

        await fire(built.sub("withdraw"), interaction)

        assert built.authorisations.forgotten == [(GUILD, interaction.user.id)]

    async def test_it_says_that_forgetting_is_not_revoking(self) -> None:
        """The honest half. Dropping this copy does not withdraw the grant on GitHub's side, and
        only the person who granted it can do that - so the reply says where."""
        built = Built()
        interaction = FakeInteraction(user=developer())

        await fire(built.sub("withdraw"), interaction)

        assert "not the same as revoking" in interaction.said
        assert "Authorized OAuth Apps" in interaction.said

    async def test_withdrawing_what_was_never_held_says_that_instead(self) -> None:
        """A repeat is not a failure, and "it is gone" would be a claim about something that was
        never there."""
        built = Built(held=False)
        interaction = FakeInteraction(user=developer())

        await fire(built.sub("withdraw"), interaction)

        assert interaction.said == NOTHING_TO_WITHDRAW

    async def test_withdrawing_asks_github_nothing(self) -> None:
        built = Built()

        await fire(built.sub("withdraw"), FakeInteraction(user=developer()))

        assert built.verification.issued_for == []


class TestShowing:
    async def test_it_asks_about_this_server_as_whoever_ran_it(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert built.boards.asked == [(GUILD, interaction.user.id)]

    async def test_a_board_being_read(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == SUCCEEDED
        assert "acme/widget mirrors acme's board #3, Roadmap" in interaction.said
        assert "read with <@42>'s GitHub authorisation" in interaction.said

    async def test_no_board_at_all(self) -> None:
        built = Built(
            boards=StubBoards(standing_is=standing(number=None, title=None, linked_by=None))
        )
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == SUCCEEDED
        assert "mirrors no board. /board link chooses one." in interaction.said

    async def test_a_board_another_server_also_claims(self) -> None:
        """Only a pair linked before boards were told apart by whose they are can be here,
        and the poll reads such a board as nobody's - so it says so, and says the way out."""
        built = Built(boards=StubBoards(standing_is=standing(title=None, shared=True)))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == OWED
        assert "and so does another server, so neither is being read" in interaction.said
        assert "/board unlink" in interaction.said

    async def test_a_board_nobody_stands_behind(self) -> None:
        """Every board linked before issue #170 looks like this until somebody links it again."""
        built = Built(
            boards=StubBoards(standing_is=standing(title=None, linked_by=None, held=False))
        )
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == OWED
        assert "nobody's authorisation stands behind it" in interaction.said

    async def test_a_linker_who_withdrew(self) -> None:
        built = Built(boards=StubBoards(standing_is=standing(title=None, held=False)))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == OWED
        assert "linked by <@42>, whose authorisation is gone" in interaction.said

    async def test_a_board_that_will_not_open(self) -> None:
        built = Built(boards=StubBoards(standing_is=standing(title=None)))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.mark == OWED
        assert "does not open with their authorisation" in interaction.said

    async def test_the_askers_own_authorisation_is_reported_to_them(self) -> None:
        built = Built(boards=StubBoards(standing_is=standing(yours="octocat")))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert "You have authorised as octocat" in interaction.said

    async def test_an_asker_who_has_not_authorised_is_told_how(self) -> None:
        built = Built()
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert "/board authorise does that" in interaction.said

    async def test_a_server_with_no_repository(self) -> None:
        built = Built(
            boards=StubBoards(
                error=NotRegisteredError("This server has no repository yet. Run /register first.")
            )
        )
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert "/register" in interaction.said

    async def test_github_being_unavailable_is_said_rather_than_raised(self) -> None:
        built = Built(boards=StubBoards(error=GitHubUnavailableError("Could not reach GitHub")))
        interaction = FakeInteraction(user=project_manager())

        await fire(built.sub("show"), interaction)

        assert interaction.said == "Could not reach GitHub"


class TestThePicker:
    def boards(self, how_many: int) -> tuple[ProjectListing, ...]:
        return tuple(ProjectListing(number=n, title=f"Board {n}") for n in range(1, how_many + 1))

    async def test_it_is_the_one_the_board_option_offers(self) -> None:
        """Attached where somebody types, and nowhere else: a picker built and never attached
        would leave the field empty with every test of the picker itself still passing."""
        built = Built(boards=StubBoards(listed=self.boards(2)))
        options = {option.name: option for option in built.sub("link").parameters}

        assert options["board"].autocomplete is True
        assert options["owner"].autocomplete is False
        suggest = built.sub("link")._params["board"].autocomplete
        assert suggest is not None
        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")
        assert [choice.value for choice in found] == ["1", "2"]

    async def test_it_offers_the_owners_boards_and_nothing_else(self) -> None:
        """No "None - stop mirroring" entry any more: unlinking is its own half."""
        suggest = _suggesting(StubBoards(listed=self.boards(2)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")

        assert [choice.name for choice in found] == ["#1 Board 1", "#2 Board 2"]

    async def test_it_asks_as_whoever_is_choosing(self) -> None:
        """The listing is made under the chooser's own authorisation, so it can include their
        private boards and nobody else's."""
        stub = StubBoards(listed=self.boards(1))
        interaction = FakeInteraction(user=project_manager())

        await _suggesting(stub)(cast(discord.Interaction, interaction), "")

        assert stub.chosen_by == [(GUILD, "", interaction.user.id)]

    async def test_it_stays_inside_discords_cap(self) -> None:
        """Discord sends a longer list back as an error rather than truncating it."""
        suggest = _suggesting(StubBoards(listed=self.boards(40)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "")

        assert len(found) == MOST_CHOICES

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

        assert [choice.value for choice in found] == ["1"]

    async def test_typing_narrows_by_number(self) -> None:
        """An owner past twenty-five boards can only reach the rest by typing."""
        suggest = _suggesting(StubBoards(listed=self.boards(40)))

        found = await suggest(cast(discord.Interaction, FakeInteraction()), "37")

        assert [choice.value for choice in found] == ["37"]

    async def test_it_reads_the_owner_typed_into_the_other_field(self) -> None:
        stub = StubBoards(listed=self.boards(1))

        await _suggesting(stub)(cast(discord.Interaction, FakeInteraction(owner="acme")), "")

        assert [owner for _, owner, _ in stub.chosen_by] == ["acme"]

    async def test_an_owner_discord_has_not_sent_is_no_owner(self) -> None:
        """`namespace` carries only the options touched so far, so this is absent as readily as
        it is empty."""
        stub = StubBoards(listed=self.boards(1))

        await _suggesting(stub)(cast(discord.Interaction, FakeInteraction()), "")

        assert [owner for _, owner, _ in stub.chosen_by] == [""]

    async def test_it_says_nothing_outside_a_server(self) -> None:
        suggest = _suggesting(StubBoards(listed=self.boards(1)))

        assert await suggest(cast(discord.Interaction, FakeInteraction(guild_id=None)), "") == []

    async def test_it_never_raises(self) -> None:
        """Discord shows an empty box whether the callback failed or the owner has no boards, and
        nobody can tell those apart, so raising makes an outage look like an empty account."""
        suggest = _suggesting(StubBoards(listing_error=RuntimeError("GitHub is down")))

        assert await suggest(cast(discord.Interaction, FakeInteraction()), "3") == []


class TestThePickersLabels:
    def test_a_long_title_is_cut_to_what_discord_will_take(self) -> None:
        """Discord refuses a choice name over a hundred characters, and refuses it by showing
        nothing at all - so one long board title used to empty the whole picker."""
        label = _label_for(ProjectListing(number=6, title="A" * 300))

        assert len(label) == MOST_LABEL
        assert label.endswith("…")

    def test_a_short_title_is_left_alone(self) -> None:
        assert _label_for(ProjectListing(number=6, title="Shannon Bot")) == "#6 Shannon Bot"

    @pytest.mark.parametrize(
        "one",
        [
            ProjectListing(number=6, title="Shannon Bot"),
            ProjectListing(number=15, title="Code Court"),
            ProjectListing(number=1, title="#1 with a hash in the title"),
            ProjectListing(number=12, title="B" * 300),
        ],
    )
    def test_the_picker_writes_the_label_the_parser_reads(self, one: ProjectListing) -> None:
        """The two halves of one bug, pinned against each other rather than against a literal:
        `_label_for` is what the picker shows and `_wanted` is what comes back. A cut label still
        reads, because the cut is at the end and the number is at the start."""
        assert _wanted(_label_for(one)) == ChosenBoard(one.number)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("3", ChosenBoard(3)),
        (" 7 ", ChosenBoard(7)),
        ("2147483647", ChosenBoard(2147483647)),
        # The shapes the picker itself writes, which is what a client hands over when the entry is
        # typed rather than committed.
        ("#6 Shannon Bot", ChosenBoard(6)),
        ("#6", ChosenBoard(6)),
        (" #12 Anki Travel the World ", ChosenBoard(12)),
        ("#6 Board #7", ChosenBoard(6)),
        ("bug", None),
        ("", None),
        ("-1", None),
        ("3.0", None),
        # Zero meant "stop mirroring" once. Nothing here means that now.
        ("0", None),
        ("#0", None),
        ("None", None),
        ("None — stop mirroring a board", None),
        # A digit from another script is a digit to Python and not to `int()`, and past the
        # 32-bit columns a number used to reach Postgres and come back as an error.
        ("²", None),
        ("#²", None),
        ("2147483648", None),
        ("99999999999", None),
        ("9" * 5000, None),
        # A hash in front of prose is not a label: the word boundary is what tells them apart.
        ("#6abc", None),
        ("#", None),
        ("# 6", None),
    ],
)
def test_what_a_typed_board_reads_as(typed: str, expected: ChosenBoard | None) -> None:
    """Two outcomes: a board, or something that is not one. A bare number carries no owner: every
    entry the picker offers is already listed under one."""
    assert _wanted(typed) == expected


@pytest.mark.parametrize(
    ("pasted", "owner"),
    [
        ("https://github.com/users/Canon-Regularis/projects/6", "Canon-Regularis"),
        ("https://github.com/orgs/acme/projects/6", "acme"),
        ("http://github.com/users/mona/projects/6", "mona"),
        ("https://www.github.com/users/mona/projects/6", "mona"),
        ("https://github.com/users/mona/projects/6/", "mona"),
        ("https://github.com/users/mona/projects/6?pane=issue", "mona"),
        ("  https://github.com/users/mona/projects/6  ", "mona"),
        ("https://GitHub.com/Users/mona/Projects/6", "mona"),
        # What the address bar actually holds: a board opens on one of its views, and that was
        # refused as "not a board" under a comment saying views were accepted.
        ("https://github.com/users/Canon-Regularis/projects/6/views/1", "Canon-Regularis"),
        ("https://github.com/orgs/acme/projects/6/views/2?layout=board", "acme"),
        ("https://github.com/users/mona/projects/6/views/12/", "mona"),
    ],
)
def test_a_pasted_board_url_is_read(pasted: str, owner: str) -> None:
    """The obvious thing to paste - and the URL carries the OWNER too, which is the other half of
    addressing a board and the half people get wrong."""
    assert _wanted(pasted) == ChosenBoard(number=6, owner=owner)


@pytest.mark.parametrize(
    "pasted",
    [
        "https://github.com/Canon-Regularis/Shannon-bot",
        "https://github.com/users/mona/projects/",
        "https://github.com/users/mona/projects/abc",
        "https://gitlab.com/users/mona/projects/6",
        "https://github.com/teams/mona/projects/6",
        "look at https://github.com/users/mona/projects/6 please",
        "https://github.com/users/mona/projects/0",
        "https://github.com/users/-mona/projects/6",
        "https://github.com/users/m%C3%B3na/projects/6",
        "https://github.com/users/mona/projects/6/views/",
        "https://github.com/users/mona/projects/6/views/table",
        "https://github.com/users/mona/projects/6/settings",
        f"https://github.com/users/{'m' * 40}/projects/6",
    ],
)
def test_something_that_is_not_a_board_url_is_refused(pasted: str) -> None:
    """Anchored at both ends, so a sentence that merely CONTAINS a board URL is turned away rather
    than half-read; and an owner that could not be a GitHub account is not one because it was
    pasted rather than typed."""
    assert _wanted(pasted) is None
