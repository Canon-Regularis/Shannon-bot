from __future__ import annotations

import pytest

from shannon.commands.sync_link import build_issue_command, build_pr_command
from shannon.config import Settings
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.permissions import PermissionGate
from shannon.domain.errors import (
    ItemNotReadyError,
    NotRegisteredError,
    RepositoryMismatchError,
    UnparseableLinkError,
)
from shannon.github.errors import GitHubNotFoundError, GitHubRateLimitError
from shannon.services.sync.manual import ManualSyncOutcome, SyncFailedError
from tests.fakes.discord_objects import (
    FakeGuildPermissions,
    FakeInteraction,
    FakeMember,
    FakeRole,
)

# /pr and /issue share their whole body, so both are driven through the same tests. What used to
# be two near-identical files could drift; this cannot. They keep their own parameter names,
# which the requirements spell out and which people type in Discord.
KINDS = [
    pytest.param(build_pr_command, "pr", "pull request", "pull/7", id="pr"),
    pytest.param(build_issue_command, "issue", "issue", "issues/7", id="issue"),
]

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


def command(service: StubManualSync, build=build_pr_command):
    return build(service, PermissionGate(Settings()))  # type: ignore[arg-type]


def member_with(role: str) -> FakeMember:
    return FakeMember(roles=[FakeRole(role)])


async def run(
    service: StubManualSync,
    member: FakeMember,
    *,
    link: str = "https://github.com/Canon-Regularis/Shannon-bot/pull/7",
    build=build_pr_command,
) -> FakeInteraction:
    interaction = FakeInteraction(guild_id=1, channel_id=99, user=member)
    await command(service, build).callback(interaction, link)
    return interaction


@pytest.mark.parametrize(("build", "name", "noun", "path"), KINDS)
async def test_a_new_item_reports_the_thread_as_opened(
    build, name: str, noun: str, path: str
) -> None:
    service = StubManualSync(outcome=OPENED)
    link = f"https://github.com/Canon-Regularis/Shannon-bot/{path}"

    interaction = await run(service, member_with("Developer"), link=link, build=build)

    assert service.calls == [{"guild_id": 1, "link": link}]
    assert interaction.reply.startswith("Opened the thread for Canon-Regularis/Shannon-bot#7")
    assert "<#555>" in interaction.reply


@pytest.mark.parametrize(("build", "name", "noun", "path"), KINDS)
async def test_a_missing_item_names_the_right_kind(build, name: str, noun: str, path: str) -> None:
    service = StubManualSync(error=GitHubNotFoundError("GitHub has nothing there"))

    interaction = await run(service, member_with("Developer"), build=build)

    assert interaction.reply == f"GitHub has no {noun} at that link."


@pytest.mark.parametrize(("build", "name", "noun", "path"), KINDS)
async def test_a_member_with_no_roles_is_rejected(build, name: str, noun: str, path: str) -> None:
    service = StubManualSync()

    interaction = await run(service, FakeMember(), build=build)

    assert service.calls == []
    assert f"You need one of these roles to use /{name}" in interaction.reply


@pytest.mark.parametrize("role", ["Developer", "Project Manager"])
async def test_the_tiers_the_table_grants_can_sync(role: str) -> None:
    service = StubManualSync()

    interaction = await run(service, member_with(role))

    assert len(service.calls) == 1
    assert "<#555>" in interaction.reply


async def test_an_administrator_can_sync() -> None:
    service = StubManualSync()

    await run(service, FakeMember(guild_permissions=FakeGuildPermissions(administrator=True)))

    assert len(service.calls) == 1


async def test_an_existing_item_reports_the_thread_as_updated() -> None:
    service = StubManualSync(outcome=UPDATED)

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply.startswith("Updated the thread for Canon-Regularis/Shannon-bot#7")


async def test_an_invalid_link_is_reported() -> None:
    service = StubManualSync(error=UnparseableLinkError("'x' is not a github.com link"))

    interaction = await run(service, member_with("Developer"), link="x")

    assert "That link did not work" in interaction.reply
    assert "not a github.com link" in interaction.reply


async def test_a_link_of_the_wrong_kind_is_reported_as_such() -> None:
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

    interaction = await run(service, member_with("Developer"))

    assert interaction.reply == "This server is registered to a/b, not c/d."


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


async def test_an_item_still_being_set_up_says_to_try_again() -> None:
    """The note path raises this. A command hitting it should say something useful."""
    service = StubManualSync(error=ItemNotReadyError("no thread yet"))

    interaction = await run(service, member_with("Developer"))

    assert "still being set up" in interaction.reply


async def test_an_unexpected_failure_is_not_swallowed() -> None:
    """Not a ShannonError, so the command lets it out for the tree's handler to answer."""
    service = StubManualSync(error=RuntimeError("connection pool exhausted"))

    with pytest.raises(RuntimeError):
        await run(service, member_with("Developer"))


async def test_running_outside_a_guild_is_refused() -> None:
    service = StubManualSync()
    interaction = FakeInteraction(guild_id=None, user=member_with("Developer"))

    await command(service).callback(interaction, "x")

    assert service.calls == []
    assert interaction.reply == "Run this inside a server channel."


async def test_the_command_defers_before_doing_slow_work() -> None:
    service = StubManualSync()
    interaction = FakeInteraction(guild_id=1, user=member_with("Developer"))

    await command(service).callback(interaction, "x")

    assert interaction.response.deferred is True
