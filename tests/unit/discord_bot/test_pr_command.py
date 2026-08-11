from __future__ import annotations

import pytest

from shannon.config import Settings
from shannon.discord_bot.commands.pr import build_pr_command
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.permissions import PermissionGate
from shannon.domain.errors import (
    NotRegisteredError,
    RepositoryMismatchError,
    UnparseableLinkError,
)
from shannon.github.errors import GitHubNotFoundError, GitHubRateLimitError
from shannon.services.manual_sync import ManualSyncOutcome, SyncFailedError
from tests.fakes.discord_objects import (
    FakeGuildPermissions,
    FakeInteraction,
    FakeMember,
    FakeRole,
)

LINK = "https://github.com/Canon-Regularis/Shannon-bot/pull/7"
OPENED = ManualSyncOutcome(
    thread_id=555, created=True, number=7, full_name="Canon-Regularis/Shannon-bot"
)
UPDATED = ManualSyncOutcome(
    thread_id=555, created=False, number=7, full_name="Canon-Regularis/Shannon-bot"
)


class StubManualSync:
    def __init__(
        self, *, outcome: ManualSyncOutcome | None = OPENED, error: Exception | None = None
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def sync_link(self, *, guild_id: int, link: str) -> ManualSyncOutcome:
        self.calls.append({"guild_id": guild_id, "link": link})
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def command(service: StubManualSync):
    return build_pr_command(service, PermissionGate(Settings()))  # type: ignore[arg-type]


def member_with(role: str) -> FakeMember:
    return FakeMember(roles=[FakeRole(role)])


async def run(service: StubManualSync, member: FakeMember, link: str = LINK) -> FakeInteraction:
    interaction = FakeInteraction(guild_id=1, channel_id=99, user=member)
    await command(service).callback(interaction, link)
    return interaction


@pytest.mark.parametrize("role", ["Developer", "Reviewer", "Project Manager"])
async def test_the_three_allowed_tiers_can_sync(role: str) -> None:
    service = StubManualSync()

    interaction = await run(service, member_with(role))

    assert service.calls == [{"guild_id": 1, "link": LINK}]
    assert "<#555>" in interaction.reply


async def test_an_administrator_can_sync() -> None:
    service = StubManualSync()

    await run(service, FakeMember(guild_permissions=FakeGuildPermissions(administrator=True)))

    assert len(service.calls) == 1


async def test_a_member_with_no_roles_is_rejected() -> None:
    service = StubManualSync()

    interaction = await run(service, FakeMember())

    assert service.calls == []
    assert "You need one of these roles to use /pr" in interaction.reply


async def test_a_new_pull_request_reports_the_thread_as_opened() -> None:
    service = StubManualSync(outcome=OPENED)

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply.startswith("Opened the thread for Canon-Regularis/Shannon-bot#7")


async def test_an_existing_pull_request_reports_the_thread_as_updated() -> None:
    service = StubManualSync(outcome=UPDATED)

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply.startswith("Updated the thread for Canon-Regularis/Shannon-bot#7")


async def test_an_invalid_link_is_reported() -> None:
    service = StubManualSync(error=UnparseableLinkError("'x' is not a github.com link"))

    interaction = await run(service, member_with("Developer"), link="x")

    assert "That link did not work" in interaction.reply


async def test_an_issue_link_is_reported_as_such() -> None:
    service = StubManualSync(
        error=UnparseableLinkError("'...' is an issue link, not a pull request link")
    )

    interaction = await run(service, member_with("Developer"))

    assert "is an issue link" in interaction.reply


async def test_an_unregistered_server_is_told_to_register_first() -> None:
    service = StubManualSync(
        error=NotRegisteredError("This server has no repository yet. Run /register first.")
    )

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply == "This server has no repository yet. Run /register first."


async def test_a_link_to_another_repository_is_refused() -> None:
    service = StubManualSync(
        error=RepositoryMismatchError("This server is registered to a/b, not c/d.")
    )

    interaction = await run(service, member_with("Reviewer"))

    assert interaction.reply == "This server is registered to a/b, not c/d."


async def test_a_missing_pull_request_is_reported() -> None:
    service = StubManualSync(error=GitHubNotFoundError("GitHub has nothing at /pulls/7"))

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply == "GitHub has no pull request at that link."


async def test_a_github_failure_is_reported_rather_than_raised() -> None:
    service = StubManualSync(error=GitHubRateLimitError("GitHub rate limit reached"))

    interaction = await run(service, member_with("Developer"))

    assert "GitHub could not be reached" in interaction.reply


async def test_a_discord_failure_is_reported_rather_than_raised() -> None:
    service = StubManualSync(error=DiscordGatewayError("Discord refused to create a thread"))

    interaction = await run(service, member_with("Developer"))

    assert "Discord refused the update" in interaction.reply


async def test_a_repository_without_a_channel_mapping_is_explained() -> None:
    service = StubManualSync(
        error=SyncFailedError(
            "The repository is registered but has no pull request channel mapped."
        )
    )

    interaction = await run(service, member_with("Developer"))

    assert "no pull request channel mapped" in interaction.reply


async def test_an_unexpected_failure_is_not_swallowed() -> None:
    service = StubManualSync(error=RuntimeError("connection pool exhausted"))

    with pytest.raises(RuntimeError):
        await run(service, member_with("Developer"))


async def test_running_outside_a_guild_is_refused() -> None:
    service = StubManualSync()
    interaction = FakeInteraction(guild_id=None, user=member_with("Developer"))

    await command(service).callback(interaction, LINK)

    assert service.calls == []
    assert interaction.reply == "Run this inside a server channel."


async def test_the_command_defers_before_doing_slow_work() -> None:
    service = StubManualSync()
    interaction = FakeInteraction(guild_id=1, user=member_with("Developer"))

    await command(service).callback(interaction, LINK)

    assert interaction.response.deferred is True
