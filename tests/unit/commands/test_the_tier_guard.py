"""The tier guard, on a server that lets GitHub stand in for a role. Issue #94.

What is worth pinning is the order things happen in, because each is a promise somebody relies on
without seeing it. A role holder is let through with no defer and no question, so every first
response a command makes after the guard is still its first. A server that never turned GitHub on
is answered word for word as before. And where GitHub is asked, the interaction was deferred
first, because Discord drops one left unanswered for three seconds and says nothing at all.

The grant itself is a fake told its answer: what GitHub would say, and why, is the grant
service's own integration tests' to check.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import cast

import discord
import pytest

from shannon.commands._guards import (
    COULD_NOT_ASK_GITHUB,
    NOT_IN_A_SERVER,
    NOT_IN_A_THREAD,
    holds_the_tier,
    in_a_server,
    in_a_thread,
)
from shannon.commands._permissions import REGISTER_ROLES, SYNC_ROLES, WORKFLOW_ROLES
from shannon.config import Settings
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import OWED, REFUSED
from shannon.discord_bot.roles import CommandRole, ConfiguredRoles
from shannon.github import people
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.fakes.grants import FakeGrants
from tests.unit.commands.conftest import (
    administrator,
    default_gate,
    developer,
    gate_with,
    project_manager,
)

pytestmark = pytest.mark.unit

GUILD = 1
ALICE = 555

# What a grant says when it turns somebody away, as the real one words it.
READ_ONLY = (
    "GitHub's write permission on acme/widgets would let you in as well, but GitHub gives octocat "
    "read access there. Triage counts as read here, which is GitHub's own folding."
)
ASIDE = "Reminding yourself needs no role."


def ran_by(member: FakeMember | None = None, *, channel_id: int | None = 10) -> FakeInteraction:
    """The command run by ALICE, who holds no role unless a test gives her one."""
    who = member or FakeMember()
    who.id = ALICE
    return FakeInteraction(guild_id=GUILD, channel_id=channel_id, user=who)


async def guarded(
    interaction: FakeInteraction,
    gate: PermissionGate,
    roles: Collection[CommandRole] = SYNC_ROLES,
    *,
    aside: str = "",
) -> bool:
    """The guard on `/pr`'s tiers unless a test says otherwise, with the stand-in cast once."""
    theirs = cast(discord.Interaction, interaction)
    return await holds_the_tier(theirs, GUILD, "pr", gate, roles, aside=aside)


class TestAMemberWithTheRole:
    @pytest.mark.parametrize(
        "caller", [developer, project_manager, administrator], ids=["dev", "pm", "admin"]
    )
    async def test_is_let_through_without_a_defer_or_a_question(
        self, caller: Callable[[], FakeMember]
    ) -> None:
        """So whatever the command answers next is still its first response."""
        grants = FakeGrants(None)
        interaction = ran_by(caller())

        assert await guarded(interaction, gate_with(grants)) is True
        assert grants.asked == []
        assert interaction.response.deferred is False
        assert interaction.replies == []


class TestTheSettingOff:
    async def test_the_refusal_is_the_one_there_always_was_and_comes_first(self) -> None:
        interaction = ran_by()

        assert await guarded(interaction, default_gate()) is False
        assert interaction.response.messages == [
            f"{REFUSED} {default_gate().denial('pr', SYNC_ROLES)}"
        ]
        assert interaction.followup.messages == []
        assert interaction.response.deferred is False

    async def test_an_aside_follows_the_denial_with_one_space(self) -> None:
        """Which is the sentence `/remind` has always ended on."""
        interaction = ran_by()

        await guarded(interaction, default_gate(), REGISTER_ROLES, aside=ASIDE)

        assert interaction.said == f"{default_gate().denial('pr', REGISTER_ROLES)} {ASIDE}"


class TestNothingGitHubCanStandIn:
    """A gate with a route turned on, and nothing it allows that GitHub stands in for."""

    async def test_an_admin_only_gate_is_refused_as_before_and_github_is_not_asked(self) -> None:
        grants = FakeGrants(None)
        interaction = ran_by()
        gate = gate_with(grants)

        await guarded(interaction, gate, frozenset({CommandRole.ADMIN}))

        assert grants.asked == []
        assert interaction.response.messages == [
            f"{REFUSED} {gate.denial('pr', frozenset({CommandRole.ADMIN}))}"
        ]
        assert interaction.response.deferred is False

    async def test_a_tier_a_server_emptied_stays_off(self) -> None:
        """Emptying a role setting is how a server turns a tier off, and GitHub cannot undo it."""
        grants = FakeGrants(None)
        interaction = ran_by()
        gate = PermissionGate(
            ConfiguredRoles.from_settings(Settings(role_developer="", role_project_manager="")),
            grants,
        )

        assert await guarded(interaction, gate) is False
        assert grants.asked == []
        assert interaction.said == "You are not allowed to use /pr."


class TestAMemberGitHubLetsIn:
    async def test_the_interaction_was_deferred_before_github_was_asked(self) -> None:
        interaction = ran_by()
        grants = FakeGrants(None, watching=interaction)

        assert await guarded(interaction, gate_with(grants)) is True
        assert grants.deferred_when_asked == [True]
        assert interaction.response.deferred_ephemerally is True
        assert interaction.replies == []

    @pytest.mark.parametrize(
        ("roles", "level"),
        [
            (SYNC_ROLES, people.WRITE),
            (WORKFLOW_ROLES, people.ADMIN),
            (REGISTER_ROLES, people.ADMIN),
        ],
        ids=["sync", "workflow", "register"],
    )
    async def test_github_is_asked_about_this_member_at_the_gates_level(
        self, roles: frozenset[CommandRole], level: str
    ) -> None:
        grants = FakeGrants(None)

        await guarded(ran_by(), gate_with(grants), roles)

        assert grants.asked == [(GUILD, ALICE, level)]

    async def test_the_server_guard_hands_back_the_guild(self) -> None:
        interaction = ran_by()

        found = await in_a_server(
            cast(discord.Interaction, interaction), "pr", gate_with(FakeGrants(None)), SYNC_ROLES
        )

        assert found == GUILD


class TestAMemberGitHubTurnsAway:
    async def test_the_denial_and_the_reason_come_as_one_private_followup(self) -> None:
        interaction = ran_by()

        assert await guarded(interaction, gate_with(FakeGrants(READ_ONLY))) is False
        assert interaction.response.messages == []
        assert interaction.followup.messages == [
            f"{REFUSED} {default_gate().denial('pr', SYNC_ROLES)} {READ_ONLY}"
        ]
        assert interaction.ephemerally == [True]

    async def test_an_aside_comes_last(self) -> None:
        interaction = ran_by()

        await guarded(interaction, gate_with(FakeGrants(READ_ONLY)), REGISTER_ROLES, aside=ASIDE)

        assert interaction.said == (
            f"{default_gate().denial('pr', REGISTER_ROLES)} {READ_ONLY} {ASIDE}"
        )

    async def test_no_refusal_ends_on_a_space(self) -> None:
        interaction = ran_by()

        await guarded(interaction, gate_with(FakeGrants(READ_ONLY)))

        assert interaction.reply == interaction.reply.strip()


class TestGitHubCouldNotBeAsked:
    """A refusal, never a grant, since this raises access."""

    @pytest.mark.parametrize(
        "error",
        [GitHubUnavailableError("GitHub is down"), GitHubAuthError("the token has expired")],
        ids=["outage", "token"],
    )
    async def test_anything_but_a_rate_limit_is_red(self, error: GitHubError) -> None:
        interaction = ran_by()

        assert await guarded(interaction, gate_with(FakeGrants(error=error))) is False
        assert interaction.mark == REFUSED
        assert interaction.said == (
            f"{default_gate().denial('pr', SYNC_ROLES)} {COULD_NOT_ASK_GITHUB}"
        )

    async def test_a_rate_limit_is_amber_because_it_comes_right(self) -> None:
        interaction = ran_by()
        spent = GitHubRateLimitError("rate limited", retry_after=60)

        assert await guarded(interaction, gate_with(FakeGrants(error=spent))) is False
        assert interaction.mark == OWED
        assert interaction.said.startswith(default_gate().denial("pr", SYNC_ROLES))

    async def test_it_is_a_private_followup_and_keeps_the_aside(self) -> None:
        interaction = ran_by()
        grants = FakeGrants(error=GitHubUnavailableError("GitHub is down"))

        await guarded(interaction, gate_with(grants), REGISTER_ROLES, aside=ASIDE)

        assert interaction.response.messages == []
        assert interaction.ephemerally == [True]
        assert interaction.said.endswith(f"{COULD_NOT_ASK_GITHUB} {ASIDE}")


class TestWhereItWasRun:
    async def test_outside_a_server_github_is_never_asked(self) -> None:
        grants = FakeGrants(None)
        interaction = ran_by()
        interaction.guild_id = None

        found = await in_a_server(
            cast(discord.Interaction, interaction), "pr", gate_with(grants), SYNC_ROLES
        )

        assert found is None
        assert grants.asked == []
        assert interaction.said == NOT_IN_A_SERVER

    async def test_in_no_channel_somebody_github_turns_away_is_told_about_the_tier(self) -> None:
        """The channel is checked after the tier, with GitHub as much as with the role."""
        interaction = ran_by(channel_id=None)

        found = await in_a_thread(
            cast(discord.Interaction, interaction),
            "pr",
            gate_with(FakeGrants(READ_ONLY)),
            SYNC_ROLES,
        )

        assert found is None
        assert interaction.said.endswith(READ_ONLY)

    async def test_in_no_channel_somebody_github_lets_in_is_told_about_the_channel(self) -> None:
        interaction = ran_by(channel_id=None)

        found = await in_a_thread(
            cast(discord.Interaction, interaction), "pr", gate_with(FakeGrants(None)), SYNC_ROLES
        )

        assert found is None
        assert interaction.followup.messages == [f"{REFUSED} {NOT_IN_A_THREAD}"]
