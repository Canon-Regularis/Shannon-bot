from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from shannon.commands._replies import reply_for
from shannon.commands.register import build_register_command
from shannon.domain.errors import DuplicateRegistrationError, UnparseableLinkError
from shannon.github.errors import GitHubNotFoundError, GitHubRateLimitError, GitHubUnavailableError
from shannon.services.registration import RegistrationResult
from tests.fakes.discord_objects import (
    FakeInteraction,
    FakeMember,
)
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

LINK = "https://github.com/Canon-Regularis/Shannon-bot"
RESULT = RegistrationResult(
    repository_id=1,
    full_name="Canon-Regularis/Shannon-bot",
    html_url=LINK,
    pr_channel_id=99,
)


class StubRegistration:
    def __init__(
        self, *, result: RegistrationResult | None = RESULT, error: Exception | None = None
    ):
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def register(self, *, guild_id: int, channel_id: int, link: str) -> RegistrationResult:
        self.calls.append({"guild_id": guild_id, "channel_id": channel_id, "link": link})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def command(service: StubRegistration):
    return build_register_command(service, default_gate())


async def run(service: StubRegistration, member: FakeMember, link: str = LINK) -> FakeInteraction:
    interaction = FakeInteraction(guild_id=1, channel_id=99, user=member)
    await command(service).callback(interaction, link)
    return interaction


async def test_a_project_manager_can_register() -> None:
    service = StubRegistration()

    interaction = await run(service, project_manager())

    assert service.calls == [{"guild_id": 1, "channel_id": 99, "link": LINK}]
    assert "Registered Canon-Regularis/Shannon-bot" in interaction.reply
    assert "<#99>" in interaction.reply


async def test_an_administrator_can_register() -> None:
    service = StubRegistration()

    await run(service, administrator())

    assert len(service.calls) == 1


async def test_a_developer_cannot_register() -> None:
    service = StubRegistration()

    interaction = await run(service, developer())

    assert service.calls == []
    assert "You need one of these roles to use /register" in interaction.reply


async def test_a_member_with_no_roles_cannot_register() -> None:
    service = StubRegistration()

    interaction = await run(service, FakeMember())

    assert service.calls == []
    assert "/register" in interaction.reply


async def test_an_invalid_link_is_reported_without_calling_github() -> None:
    service = StubRegistration(error=UnparseableLinkError("'x' is not a github.com link"))

    interaction = await run(service, project_manager(), link="x")

    assert "That link did not work" in interaction.reply
    assert "not a github.com link" in interaction.reply


async def test_a_missing_repository_is_reported_plainly() -> None:
    service = StubRegistration(error=GitHubNotFoundError("GitHub has nothing at /repos/a/b"))

    interaction = await run(service, project_manager())

    assert interaction.reply == "GitHub could not find that repository."


async def test_a_guild_that_already_registered_is_told_so() -> None:
    service = StubRegistration(
        error=DuplicateRegistrationError("This server is already registered to owner/other")
    )

    interaction = await run(service, project_manager())

    assert interaction.reply == "This server is already registered to owner/other"


async def test_a_repository_bound_elsewhere_is_told_so() -> None:
    service = StubRegistration(
        error=DuplicateRegistrationError("owner/repo is already registered to another server")
    )

    interaction = await run(service, project_manager())

    assert "already registered to another server" in interaction.reply


@pytest.mark.parametrize(
    "error",
    [
        GitHubRateLimitError("GitHub rate limit reached"),
        GitHubUnavailableError("GitHub returned 502 for /repos/a/b"),
    ],
)
async def test_github_trouble_is_reported_rather_than_raised(error: Exception) -> None:
    """What matters here is that the person is told at all. Which words they get is the reply
    table's business, and it says something different for a spent quota than for an outage."""
    service = StubRegistration(error=error)

    interaction = await run(service, project_manager())

    assert interaction.reply == reply_for(error)
    assert "GitHub" in interaction.reply


async def test_a_database_failure_is_not_swallowed() -> None:
    """An unexpected failure should surface, not be reported to the user as a link problem."""
    service = StubRegistration(error=RuntimeError("connection pool exhausted"))

    with pytest.raises(RuntimeError):
        await run(service, project_manager())


async def test_running_outside_a_guild_is_refused() -> None:
    service = StubRegistration()
    interaction = FakeInteraction(guild_id=None, channel_id=None, user=administrator())

    await command(service).callback(interaction, LINK)

    assert service.calls == []
    assert interaction.reply == "Run this inside a server channel."


async def test_the_command_defers_before_doing_slow_work() -> None:
    service = StubRegistration()
    interaction = FakeInteraction(guild_id=1, channel_id=99, user=project_manager())

    await command(service).callback(interaction, LINK)

    assert interaction.response.deferred is True
    assert interaction.followup.messages != []


async def test_a_rejected_user_is_answered_without_deferring() -> None:
    service = StubRegistration()
    interaction = FakeInteraction(guild_id=1, channel_id=99, user=developer())

    await command(service).callback(interaction, LINK)

    assert interaction.response.deferred is False
    assert interaction.response.messages != []


async def test_registering_where_threads_cannot_be_opened_is_refused() -> None:
    """The channel becomes the home for pull request threads, so it has to be able to hold one.

    A slash command run inside a thread reports that thread as its channel. Catching it here is
    the last moment somebody is watching; the sync path reaches it hours later with nobody left
    to tell.
    """
    service = StubRegistration()
    interaction = FakeInteraction(
        guild_id=1, channel_id=99, user=project_manager(), channel=MagicMock(spec=discord.Thread)
    )

    await command(service).callback(interaction, LINK)

    assert "text or forum channel" in interaction.reply
    assert service.calls == []


async def test_a_forum_channel_is_accepted() -> None:
    service = StubRegistration()
    interaction = FakeInteraction(
        guild_id=1,
        channel_id=99,
        user=project_manager(),
        channel=MagicMock(spec=discord.ForumChannel),
    )

    await command(service).callback(interaction, LINK)

    assert service.calls == [{"guild_id": 1, "channel_id": 99, "link": LINK}]
