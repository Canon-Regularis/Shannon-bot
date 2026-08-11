from __future__ import annotations

import pytest

from shannon.config import Settings
from shannon.discord_bot.commands.link import build_link_command
from shannon.discord_bot.permissions import PermissionGate
from shannon.services.linking import InvalidGitHubUsernameError
from tests.fakes.discord_objects import (
    FakeGuildPermissions,
    FakeInteraction,
    FakeMember,
    FakeRole,
)


class StubLinking:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> str:
        self.calls.append(
            {
                "guild_id": guild_id,
                "github_username": github_username,
                "discord_user_id": discord_user_id,
            }
        )
        if self.error is not None:
            raise self.error
        return github_username.lower().lstrip("@")


def command(service: StubLinking):
    return build_link_command(service, PermissionGate(Settings()))  # type: ignore[arg-type]


async def test_anyone_can_link_their_own_account() -> None:
    service = StubLinking()
    me = FakeMember(id=42)
    interaction = FakeInteraction(guild_id=1, user=me)

    await command(service).callback(interaction, "octocat")

    assert service.calls == [{"guild_id": 1, "github_username": "octocat", "discord_user_id": 42}]
    assert "Linked GitHub user octocat to <@42>" in interaction.reply


async def test_linking_someone_else_needs_a_project_manager() -> None:
    service = StubLinking()
    interaction = FakeInteraction(guild_id=1, user=FakeMember(id=42, roles=[FakeRole("Developer")]))

    await command(service).callback(interaction, "octocat", FakeMember(id=99))

    assert service.calls == []
    assert "You need one of these roles to use /link" in interaction.reply


async def test_a_project_manager_can_link_someone_else() -> None:
    service = StubLinking()
    interaction = FakeInteraction(
        guild_id=1, user=FakeMember(id=42, roles=[FakeRole("Project Manager")])
    )

    await command(service).callback(interaction, "octocat", FakeMember(id=99))

    assert service.calls[0]["discord_user_id"] == 99


async def test_an_administrator_can_link_someone_else() -> None:
    service = StubLinking()
    interaction = FakeInteraction(
        guild_id=1,
        user=FakeMember(id=42, guild_permissions=FakeGuildPermissions(administrator=True)),
    )

    await command(service).callback(interaction, "octocat", FakeMember(id=99))

    assert service.calls[0]["discord_user_id"] == 99


async def test_a_bad_username_is_reported() -> None:
    service = StubLinking(
        error=InvalidGitHubUsernameError("'not a name' is not a GitHub username.")
    )
    interaction = FakeInteraction(guild_id=1, user=FakeMember(id=42))

    await command(service).callback(interaction, "not a name")

    assert interaction.reply == "'not a name' is not a GitHub username."


async def test_running_outside_a_guild_is_refused() -> None:
    service = StubLinking()
    interaction = FakeInteraction(guild_id=None, user=FakeMember(id=42))

    await command(service).callback(interaction, "octocat")

    assert service.calls == []
    assert interaction.reply == "Run this inside a server channel."


@pytest.mark.parametrize("given", ["@octocat", "  octocat  "])
async def test_the_username_reaches_the_service_as_typed(given: str) -> None:
    """Trimming is the service's job, so the command must not quietly do its own."""
    service = StubLinking()
    interaction = FakeInteraction(guild_id=1, user=FakeMember(id=42))

    await command(service).callback(interaction, given)

    assert service.calls[0]["github_username"] == given
