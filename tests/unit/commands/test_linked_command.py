"""`/linked`, as somebody asking about their own link meets it. Issue #232.

Every state a member can be in is told apart here, word for word, because the point of the command
is that "linked" has states which look alike from outside. The facts are a stub's, so this pins
the wording and who is asked about; `test_what_a_member_is_linked_to.py` pins the facts against a
real database, and `test_asking_whether_you_are_linked.py` the two together.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import discord
import pytest

from shannon.commands.linked import build_linked_command
from shannon.db.stores.identities import ProvedAccount
from shannon.db.stores.user_links import LinkedAccount, LinkRecord
from shannon.discord_bot.responses import REFUSED
from shannon.discord_bot.slash import SlashCommand
from shannon.services.linking import LinkStanding
from tests.fakes.discord_objects import FakeGuildPermissions, FakeInteraction, FakeMember

ALICE = 42
REPO = "octo-org/widgets"
SINCE = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)
DAY = f"<t:{int(SINCE.timestamp())}:D>"

OCTOCAT = LinkRecord(account=LinkedAccount(login="octocat", github_user_id=583231), linked_at=SINCE)
BY_NAME = LinkRecord(account=LinkedAccount(login="octocat", github_user_id=None), linked_at=SINCE)


def signed_in_as(login: str, github_user_id: int) -> ProvedAccount:
    return ProvedAccount(login=login, github_user_id=github_user_id, verified_at=SINCE)


AS_OCTOCAT = signed_in_as("octocat", 583231)
AS_MONALISA = signed_in_as("monalisa", 200)
# The name octocat, held now by an account that is not the one linked.
AS_THE_NEW_OCTOCAT = signed_in_as("OctoCat", 999)

MENTIONED = (
    f"-# This server mirrors {REPO}, and its threads mention you where GitHub names octocat."
)
NOT_THROUGH_THIS_LINK = (
    f"-# This server mirrors {REPO}, but its threads cannot mention you through this link."
)
UNLINKED = (
    f"-# This server mirrors {REPO}, but nothing here links you to a GitHub account, so its "
    "threads cannot mention you."
)
CANNOT_LINK = (
    "This bot cannot link anybody until an admin finishes setting up sign-in with GitHub and "
    "Discord."
)


def standing(
    link: LinkRecord | None = None, proof: ProvedAccount | None = None, *, repo: str | None = REPO
) -> LinkStanding:
    return LinkStanding(repo_name=repo, link=link, proof=proof)


# Every state but the one GitHub vouched for, each owing the member something to run.
OWING = [
    standing(OCTOCAT),
    standing(OCTOCAT, AS_MONALISA),
    standing(OCTOCAT, AS_THE_NEW_OCTOCAT),
    standing(BY_NAME),
    standing(None, AS_OCTOCAT),
    standing(),
]
OWING_NAMES = [
    "never signed in",
    "signed in as another",
    "the name changed hands",
    "by name alone",
    "signed in, never linked",
    "neither",
]
EVERY_STATE = [standing(OCTOCAT, AS_OCTOCAT), *OWING, standing(repo=None)]
STATE_NAMES = ["proved", *OWING_NAMES, "no repository"]


class StubLinks:
    """Answers with whatever a test gave it, and remembers who it was asked about."""

    def __init__(self, answer: LinkStanding) -> None:
        self.answer = answer
        self.asked: list[tuple[int, int]] = []

    async def standing(self, *, guild_id: int, discord_user_id: int) -> LinkStanding:
        self.asked.append((guild_id, discord_user_id))
        return self.answer


@dataclass
class Linking:
    """Whether this deployment can hand out a link, as the verification service says."""

    can_prove_identity: bool = True


Run = Callable[[discord.Interaction], Awaitable[None]]


async def fire(
    service: StubLinks, *, guild_id: int | None = 1, can_link: bool = True
) -> FakeInteraction:
    """Run the command as a member holding no role, with discord.py's typing answered once.

    A cast rather than the `type: ignore[arg-type]` the other command tests carry: this command
    takes no options, so mypy reads a bare call as missing them and reports `call-arg`, which that
    ignore would leave standing beside an ignore that is never used.
    """
    nobody = FakeMember(id=ALICE, roles=[], guild_permissions=FakeGuildPermissions())
    interaction = FakeInteraction(user=nobody, guild_id=guild_id)
    command: SlashCommand = build_linked_command(service, Linking(can_prove_identity=can_link))
    await cast(Run, command.callback)(cast(discord.Interaction, interaction))
    return interaction


async def told(answer: LinkStanding, *, can_link: bool = True) -> str:
    return (await fire(StubLinks(answer), can_link=can_link)).said


class TestAnAccountGitHubVouchedFor:
    async def test_it_names_the_account_and_the_day_and_the_one_thing_it_cannot_see(
        self,
    ) -> None:
        """Everything here is stored, so a rename since is the one thing it cannot know, and
        the one thing that would stop their threads finding them."""
        assert await told(standing(OCTOCAT, AS_OCTOCAT)) == (
            f"You have been linked to octocat on GitHub since {DAY}, and GitHub has vouched that "
            "it is yours. If you have renamed your GitHub account since, run /link so this server "
            f"knows.\n\n{MENTIONED}"
        )

    async def test_a_proof_given_under_another_name_for_the_same_account_still_proves_it(
        self,
    ) -> None:
        """The account was renamed between signing in and linking. The account is the one that
        was proved, so the answer is the same, and the name it had then is nobody's business."""
        renamed = await told(standing(OCTOCAT, signed_in_as("octocat-before", 583231)))

        assert renamed == await told(standing(OCTOCAT, AS_OCTOCAT))


class TestALinkNothingProves:
    async def test_one_made_without_signing_in_says_how_to_prove_it(self) -> None:
        assert await told(standing(OCTOCAT)) == (
            f"You have been linked to octocat on GitHub since {DAY}, but nothing proves it is "
            "yours: it was linked without signing in to GitHub. Run /link and sign in as octocat "
            f"to prove it.\n\n{MENTIONED}"
        )

    async def test_a_later_sign_in_as_another_account_names_both_and_both_ways_out(self) -> None:
        """Signing in for /register or /board as another account replaced the proof. Which of
        the two they meant is theirs to say, so both ways out are offered."""
        assert await told(standing(OCTOCAT, AS_MONALISA)) == (
            f"You have been linked to octocat on GitHub since {DAY}, but you last signed in to "
            "GitHub from here as monalisa, so nothing proves octocat is yours. Run /link and sign "
            "in as octocat to prove it, or as monalisa to link that account instead."
            f"\n\n{MENTIONED}"
        )


class TestANameThatChangedHands:
    """The name the link was stored under now belongs to another account, and they signed in as
    that one. Found reviewing #232: told as signing in as somebody else, it read "nothing proves
    octocat is yours… sign in as octocat… or as octocat"."""

    async def test_it_says_the_name_moved_and_how_to_link_the_account_they_hold(self) -> None:
        assert await told(standing(OCTOCAT, AS_THE_NEW_OCTOCAT)) == (
            f"You have been linked to octocat on GitHub since {DAY}, but the OctoCat you last "
            "signed in to GitHub from here as is a different account: GitHub hands a name on once "
            "the account holding it is renamed or deleted. Run /link to link the account you hold "
            f"now.\n\n{NOT_THROUGH_THIS_LINK}"
        )

    async def test_the_same_name_in_the_same_case_is_the_same_state(self) -> None:
        """A link is stored lowercased and a proof keeps GitHub's case, so case says nothing."""
        lowercased = await told(standing(OCTOCAT, signed_in_as("octocat", 999)))

        assert lowercased == (await told(standing(OCTOCAT, AS_THE_NEW_OCTOCAT))).replace(
            "OctoCat", "octocat"
        )


class TestALinkByNameAlone:
    async def test_it_says_so_and_that_link_settles_it(self) -> None:
        assert await told(standing(BY_NAME)) == (
            f"You have been linked to octocat on GitHub by name alone since {DAY}, from before "
            f"this bot asked GitHub whose login it was. Run /link to settle it.\n\n"
            f"{NOT_THROUGH_THIS_LINK}"
        )

    async def test_a_proof_changes_nothing_about_it(self) -> None:
        """There is no account id to hold a proof against, so one in the same name is not news."""
        assert await told(standing(BY_NAME, AS_OCTOCAT)) == await told(standing(BY_NAME))


class TestNoLinkAtAll:
    async def test_somebody_who_never_linked_is_told_how(self) -> None:
        assert await told(standing()) == (
            "You are not linked to a GitHub account in this server. Run /link to connect yours: "
            "you will get a private link to sign in with, and clicking it is the whole of it."
            f"\n\n{UNLINKED}"
        )

    async def test_a_sign_in_for_another_command_is_not_taken_for_a_link(self) -> None:
        """The likeliest "I thought I was linked": GitHub knows them from /register, and this
        server holds no link."""
        assert await told(standing(None, AS_OCTOCAT)) == (
            "You are not linked to a GitHub account in this server, although you last signed in "
            "to GitHub from here as octocat. Only /link links an account, so run it to link that "
            f"one.\n\n{UNLINKED}"
        )


class TestWhereNothingCanBeLinked:
    """A deployment that cannot run the round trip: `/link` refuses there, so sending anybody to
    it would be sending them to a refusal. Found reviewing #232."""

    @pytest.mark.parametrize("answer", OWING, ids=OWING_NAMES)
    async def test_whatever_is_owed_waits_on_an_admin_rather_than_on_link(
        self, answer: LinkStanding
    ) -> None:
        said, small_print = (await told(answer, can_link=False)).split("\n\n")

        assert said.endswith(f" {CANNOT_LINK}")
        assert "/link" not in said
        assert small_print == (await told(answer)).split("\n\n")[1]

    async def test_a_link_github_vouched_for_is_left_to_stand(self) -> None:
        """Nothing is owed, and a rename could not be put right here anyway."""
        assert await told(standing(OCTOCAT, AS_OCTOCAT), can_link=False) == (
            f"You have been linked to octocat on GitHub since {DAY}, and GitHub has vouched that "
            f"it is yours.\n\n{MENTIONED}"
        )


class TestTheRepository:
    @pytest.mark.parametrize(
        "link", [OCTOCAT, BY_NAME, None], ids=["linked", "by name alone", "not linked"]
    )
    async def test_a_server_mirroring_nothing_says_so_whatever_the_link(
        self, link: LinkRecord | None
    ) -> None:
        """A link belongs to the server, so it stands with no repository and carries over to
        whichever one is registered next."""
        said = await told(standing(link, repo=None))

        assert said.endswith("\n\n-# This server mirrors no repository yet.")

    async def test_the_repository_is_small_print_under_the_answer(self) -> None:
        answer, small_print = (await told(standing(OCTOCAT, AS_OCTOCAT))).split("\n\n")

        assert not answer.startswith("-#")
        assert small_print == MENTIONED


class TestWhoMayRunItAndWhoHearsIt:
    async def test_anybody_may_run_it(self) -> None:
        """No role is asked for: a member holding none is answered rather than refused. That
        it takes no gate at all is held by `test_permission_tables` against `UNGATED`."""
        interaction = await fire(StubLinks(standing()))

        assert interaction.said.startswith("You are not linked to a GitHub account")

    async def test_it_asks_about_whoever_ran_it_in_the_server_it_was_run_in(self) -> None:
        service = StubLinks(standing())

        await fire(service, guild_id=7)

        assert service.asked == [(7, ALICE)]

    def test_there_is_no_member_to_name(self) -> None:
        """Another member's link names their GitHub account, which is theirs to see."""
        assert build_linked_command(StubLinks(standing()), Linking()).parameters == []

    def test_it_is_offered_in_servers_only(self) -> None:
        assert build_linked_command(StubLinks(standing()), Linking()).guild_only

    async def test_outside_a_server_it_says_so_and_reads_nothing(self) -> None:
        service = StubLinks(standing())

        interaction = await fire(service, guild_id=None)

        assert interaction.said == "Run this inside a server channel."
        assert interaction.mark == REFUSED
        assert interaction.ephemerally == [True]
        assert service.asked == []

    @pytest.mark.parametrize("answer", EVERY_STATE, ids=STATE_NAMES)
    async def test_every_answer_is_private(self, answer: LinkStanding) -> None:
        """It names their GitHub account, and the channel it was run in is anybody's."""
        interaction = await fire(StubLinks(answer))

        assert interaction.response.deferred_ephemerally
        assert interaction.ephemerally == [True]

    @pytest.mark.parametrize("answer", EVERY_STATE, ids=STATE_NAMES)
    async def test_no_answer_carries_a_mark(self, answer: LinkStanding) -> None:
        """It answers a question and changes nothing, so there is nothing to tick or warn about."""
        assert (await fire(StubLinks(answer))).mark == ""
