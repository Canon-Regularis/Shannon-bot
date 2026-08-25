"""Pointing a Discord role at a GitHub team.

Gated like `/set_channel` rather than like `/link`, and the difference is the point. Anybody may
claim their own GitHub account, because it is theirs. Nobody speaks for a team that way: this
decides who a whole role gets pinged for, which is a decision about the server.
"""

from __future__ import annotations

from typing import Any

import discord
import pytest
from discord import app_commands

from shannon.commands.link_team import build_link_team_command
from shannon.services.linking import InvalidGitHubTeamError
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
    administrator,
    default_gate,
    developer,
    member_with,
    project_manager,
)

ROLE_ID = 777000


class FakeRole:
    """Enough of discord.Role for the command to run without Discord."""

    def __init__(
        self, role_id: int = ROLE_ID, *, default: bool = False, mentionable: bool = True
    ) -> None:
        self.id = role_id
        self.mentionable = mentionable
        self._default = default

    def is_default(self) -> bool:
        return self._default


class StubTeamLinking:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> str:
        self.calls.append(
            {"guild_id": guild_id, "github_team": github_team, "discord_role_id": discord_role_id}
        )
        if self.error is not None:
            raise self.error
        return github_team.strip().lstrip("@").lower()


def command(service: StubTeamLinking) -> app_commands.Command:
    return build_link_team_command(service, default_gate())


async def run(
    service: StubTeamLinking,
    member: FakeMember,
    *,
    team: str = "backend",
    role: FakeRole | None = None,
    guild_id: int | None = 1,
) -> FakeInteraction:
    interaction = FakeInteraction(user=member, guild_id=guild_id)
    await command(service).callback(interaction, team, role or FakeRole())
    return interaction


def said(interaction: FakeInteraction) -> str:
    return (interaction.followup.messages + interaction.response.messages)[0]


async def test_a_team_is_pointed_at_a_role() -> None:
    service = StubTeamLinking()

    interaction = await run(service, project_manager())

    assert service.calls == [{"guild_id": 1, "github_team": "backend", "discord_role_id": ROLE_ID}]
    assert said(interaction) == (f"Reviews asked of the backend team will now ping <@&{ROLE_ID}>.")


@pytest.mark.parametrize("who", [project_manager, administrator])
async def test_project_managers_and_administrators_may_link(who) -> None:
    service = StubTeamLinking()

    await run(service, who())

    assert service.calls, "somebody the permissions table allows was refused"


@pytest.mark.parametrize("who", [developer, lambda: member_with("Reviewer")])
async def test_nobody_else_may(who) -> None:
    """Unlike /link, there is no claim-your-own case: a team is not anybody's to claim."""
    service = StubTeamLinking()

    interaction = await run(service, who())

    assert service.calls == []
    assert "You need one of these roles" in said(interaction)


async def test_everyone_is_refused_as_a_review_team() -> None:
    """The role Discord gives every member. Pinging it is the thing the mention rules exist to
    make impossible, and a server that did this once would turn every review into a broadcast."""
    service = StubTeamLinking()

    interaction = await run(service, project_manager(), role=FakeRole(default=True))

    assert service.calls == []
    assert said(interaction) == "Pick a real role. Everyone is not a review team."


async def test_it_refuses_outside_a_server() -> None:
    service = StubTeamLinking()

    interaction = await run(service, project_manager(), guild_id=None)

    assert service.calls == []
    assert said(interaction) == "Run this inside a server channel."


async def test_something_that_is_not_a_team_comes_back_as_a_sentence() -> None:
    """The interaction is deferred by this point, so anything escaping leaves the person who ran
    it watching a spinner until Discord gives up."""
    service = StubTeamLinking(error=InvalidGitHubTeamError("'not a team' is not a GitHub team."))

    interaction = await run(service, project_manager(), team="not a team")

    assert said(interaction) == "'not a team' is not a GitHub team."


class TestARoleTheBotCannotActuallyPing:
    """Discord notifies a role's members only if the role is mentionable or the sender may
    mention any role. Roles are created not mentionable and neither is in the permission list
    the README gives, so on an ordinary server the ping renders as a blue pill and reaches
    nobody. It looks exactly like it worked, which is why nobody finds out for days.

    The ping is claimed before it is sent and stamped as spent whether or not anybody read it,
    so every review request that goes past before somebody fixes this is silent.
    """

    async def test_the_answer_says_nobody_will_be_notified(self) -> None:
        service = StubTeamLinking()

        interaction = await run(service, project_manager(), role=FakeRole(mentionable=False))

        assert service.calls != [], "the link is still worth having, it is the ping that is not"
        assert "Nobody will be notified" in said(interaction)
        assert "mentionable" in said(interaction)

    async def test_a_mentionable_role_is_not_warned_about(self) -> None:
        service = StubTeamLinking()

        interaction = await run(service, project_manager())

        assert "Nobody will be notified" not in said(interaction)

    async def test_a_bot_allowed_to_mention_any_role_is_not_warned_about(self) -> None:
        """The other half of Discord's rule, and the other way to fix it."""
        service = StubTeamLinking()
        interaction = FakeInteraction(
            user=project_manager(),
            guild_id=1,
            app_permissions=discord.Permissions(mention_everyone=True),
        )

        await command(service).callback(interaction, "backend", FakeRole(mentionable=False))

        assert "Nobody will be notified" not in said(interaction)
