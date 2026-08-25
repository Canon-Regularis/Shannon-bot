from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from shannon.commands.set_channel import build_set_channel_command
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError
from shannon.services.channels import ChannelAssignment
from tests.fakes.discord_objects import (
    FakeGuildPermissions,
    FakeInteraction,
    FakeMember,
    FakeRole,
)
from tests.unit.commands.conftest import default_gate, project_manager


class StubChannels:
    def __init__(self, *, replaced: int | None = None, error: Exception | None = None) -> None:
        self.replaced = replaced
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def assign(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> ChannelAssignment:
        self.calls.append(
            {"guild_id": guild_id, "object_type": object_type, "channel_id": channel_id}
        )
        if self.error is not None:
            raise self.error
        return ChannelAssignment(
            repository_name="Canon-Regularis/Shannon-bot",
            object_type=object_type,
            discord_channel_id=channel_id,
            replaced=self.replaced,
        )


def command(service: StubChannels):
    return build_set_channel_command(service, default_gate())


def choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name="issues" if value == "ISSUE" else "pull requests", value=value)


def text_channel(channel_id: int = 4242) -> MagicMock:
    stub = MagicMock(spec=discord.TextChannel)
    stub.id = channel_id
    return stub


def forum_channel(channel_id: int = 7777, *, requires_a_tag: bool = False) -> MagicMock:
    stub = MagicMock(spec=discord.ForumChannel)
    stub.id = channel_id
    # Set explicitly. Left to the mock this is a truthy Mock, so every forum would look like one
    # demanding a tag, and a forum that demands one is refused.
    stub.flags = discord.ChannelFlags(require_tag=requires_a_tag)
    return stub


async def test_a_project_manager_can_map_the_issue_channel() -> None:
    service = StubChannels()
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel())

    assert service.calls == [{"guild_id": 1, "object_type": ObjectType.ISSUE, "channel_id": 4242}]
    assert "<#4242>" in interaction.reply


async def test_an_administrator_can_map_a_channel() -> None:
    service = StubChannels()
    interaction = FakeInteraction(
        guild_id=1, user=FakeMember(guild_permissions=FakeGuildPermissions(administrator=True))
    )

    await command(service).callback(interaction, choice("PR"), text_channel())

    assert service.calls[0]["object_type"] is ObjectType.PR


async def test_a_developer_cannot_map_a_channel() -> None:
    service = StubChannels()
    interaction = FakeInteraction(guild_id=1, user=FakeMember(roles=[FakeRole("Developer")]))

    await command(service).callback(interaction, choice("ISSUE"), text_channel())

    assert service.calls == []
    assert "You need one of these roles to use /set_channel" in interaction.reply


async def test_replacing_a_mapping_says_what_happens_to_the_open_threads() -> None:
    """Discord cannot move a thread between channels, and every tracked item keeps the one it
    has. Saying it moved would send an admin looking for threads that never went anywhere."""
    service = StubChannels(replaced=1111)
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel())

    assert "Threads already open stay in <#1111>" in interaction.reply
    assert "moved" not in interaction.reply.lower()


async def test_it_names_the_new_channel_and_the_kind_of_item() -> None:
    service = StubChannels(replaced=1111)
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel(4242))

    assert interaction.reply.startswith("Issues for Canon-Regularis/Shannon-bot will now appear")
    assert "<#4242>" in interaction.reply


async def test_remapping_to_the_same_channel_mentions_no_other_one() -> None:
    service = StubChannels(replaced=4242)
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel(4242))

    assert "already open" not in interaction.reply


async def test_a_forum_channel_is_accepted() -> None:
    service = StubChannels()
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), forum_channel())

    assert service.calls[0]["channel_id"] == 7777


async def test_a_forum_that_demands_a_tag_is_refused_here_rather_than_behind_the_queue() -> None:
    """Nothing picks a tag, so Discord refuses every post, with a 400 the queue retries.

    Left to the sync path that costs the item sixteen attempts over two hours and then drops it,
    with one log line and nobody told. Here there is somebody reading the answer.
    """
    service = StubChannels()
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(
        interaction, choice("ISSUE"), forum_channel(requires_a_tag=True)
    )

    assert service.calls == [], "it was mapped to a channel that will refuse every thread"
    assert "Require Tags" in interaction.reply


async def test_a_channel_that_cannot_hold_threads_is_refused() -> None:
    service = StubChannels()
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 5555
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), voice)

    assert service.calls == []
    assert "cannot hold threads" in interaction.reply


async def test_an_unregistered_server_is_told_to_register_first() -> None:
    service = StubChannels(
        error=NotRegisteredError("This server has no repository yet. Run /register first.")
    )
    interaction = FakeInteraction(guild_id=1, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel())

    assert interaction.reply == "This server has no repository yet. Run /register first."


async def test_running_outside_a_guild_is_refused() -> None:
    service = StubChannels()
    interaction = FakeInteraction(guild_id=None, user=project_manager())

    await command(service).callback(interaction, choice("ISSUE"), text_channel())

    assert service.calls == []
    assert interaction.reply == "Run this inside a server channel."


def test_every_kind_this_bot_mirrors_can_be_given_a_channel() -> None:
    """All three now. Tickets were held back until there was a project board to fill them.

    Tickets matter most here of the three: pull requests get a channel from /register and issues
    fall back to it, but a ticket with no mapping has nowhere to go at all.
    """
    from shannon.commands.set_channel import CHOICES

    assert [c.value for c in CHOICES] == ["PR", "ISSUE", "TICKET"]


@pytest.mark.parametrize("value", ["PR", "ISSUE", "TICKET"])
def test_every_offered_choice_is_a_real_object_type(value: str) -> None:
    assert ObjectType(value)


def test_nothing_this_bot_mirrors_is_left_off_the_list() -> None:
    """The other direction, so a fourth kind cannot be added and quietly left unmappable."""
    from shannon.commands.set_channel import CHOICES

    assert {c.value for c in CHOICES} == {kind.value for kind in ObjectType}
