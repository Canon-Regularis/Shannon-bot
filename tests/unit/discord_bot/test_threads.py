from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    ThreadNotFoundError,
)
from shannon.discord_bot.threads import (
    THREAD_NAME_LIMIT,
    DiscordThreadGateway,
    truncate_thread_name,
)


def message(message_id: int) -> MagicMock:
    stub = MagicMock(spec=discord.Message)
    stub.id = message_id
    stub.edit = AsyncMock()
    return stub


def thread(thread_id: int = 500, name: str = "#7 Add the webhook endpoint") -> MagicMock:
    stub = MagicMock(spec=discord.Thread)
    stub.id = thread_id
    stub.name = name
    stub.send = AsyncMock(return_value=message(900))
    stub.edit = AsyncMock()
    stub.fetch_message = AsyncMock(return_value=message(600))
    return stub


def text_channel(created: MagicMock) -> MagicMock:
    stub = MagicMock(spec=discord.TextChannel)
    stub.create_thread = AsyncMock(return_value=created)
    return stub


def forum_channel(created: MagicMock, created_message: MagicMock) -> MagicMock:
    stub = MagicMock(spec=discord.ForumChannel)
    stub.create_thread = AsyncMock(return_value=MagicMock(thread=created, message=created_message))
    return stub


def client_with(channel: object) -> MagicMock:
    stub = MagicMock(spec=discord.Client)
    stub.get_channel = MagicMock(return_value=channel)
    stub.fetch_channel = AsyncMock(return_value=channel)
    return stub


async def test_text_channel_thread_is_created_with_its_metadata_message() -> None:
    created = thread()
    channel = text_channel(created)
    gateway = DiscordThreadGateway(client_with(channel))

    handle = await gateway.create(channel_id=10, name="#7 Title", content="metadata")

    channel.create_thread.assert_awaited_once()
    assert channel.create_thread.await_args.kwargs["name"] == "#7 Title"
    created.send.assert_awaited_once_with("metadata")
    assert handle.thread_id == 500
    assert handle.message_id == 900


async def test_forum_channel_thread_carries_its_content_in_the_starter_post() -> None:
    created = thread()
    channel = forum_channel(created, message(901))
    gateway = DiscordThreadGateway(client_with(channel))

    handle = await gateway.create(channel_id=10, name="#7 Title", content="metadata")

    assert channel.create_thread.await_args.kwargs["content"] == "metadata"
    assert handle == type(handle)(thread_id=500, message_id=901)


async def test_creating_in_a_voice_channel_is_refused() -> None:
    gateway = DiscordThreadGateway(client_with(MagicMock(spec=discord.VoiceChannel)))

    with pytest.raises(ChannelNotFoundError, match="cannot hold threads"):
        await gateway.create(channel_id=10, name="x", content="y")


async def test_an_unreachable_channel_is_reported() -> None:
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "missing"))
    gateway = DiscordThreadGateway(client)

    with pytest.raises(ChannelNotFoundError):
        await gateway.create(channel_id=10, name="x", content="y")


async def test_update_edits_the_existing_metadata_message() -> None:
    existing = thread()
    edited = message(600)
    existing.fetch_message = AsyncMock(return_value=edited)
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=600, name=existing.name, content="new metadata"
    )

    edited.edit.assert_awaited_once_with(content="new metadata")
    existing.send.assert_not_awaited()
    assert handle.message_id == 600


async def test_update_renames_only_when_the_title_changed() -> None:
    existing = thread(name="#7 Old title")
    gateway = DiscordThreadGateway(client_with(existing))

    await gateway.update(thread_id=500, message_id=600, name="#7 Old title", content="x")
    existing.edit.assert_not_awaited()

    await gateway.update(thread_id=500, message_id=600, name="#7 New title", content="x")
    existing.edit.assert_awaited_once_with(name="#7 New title")


async def test_update_posts_a_replacement_when_the_message_was_deleted() -> None:
    existing = thread()
    existing.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=600, name=existing.name, content="metadata"
    )

    existing.send.assert_awaited_once_with("metadata")
    assert handle.message_id == 900


async def test_update_posts_a_first_message_when_none_was_stored() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=None, name=existing.name, content="metadata"
    )

    existing.fetch_message.assert_not_awaited()
    existing.send.assert_awaited_once_with("metadata")
    assert handle.message_id == 900


async def test_update_never_creates_a_second_thread() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    await gateway.update(thread_id=500, message_id=600, name="#7 Renamed", content="x")

    assert not hasattr(existing, "create_thread") or not existing.create_thread.await_count


async def test_a_missing_thread_is_reported() -> None:
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    gateway = DiscordThreadGateway(client)

    with pytest.raises(ThreadNotFoundError):
        await gateway.update(thread_id=500, message_id=None, name="x", content="y")


async def test_a_channel_that_is_not_a_thread_is_refused() -> None:
    gateway = DiscordThreadGateway(client_with(MagicMock(spec=discord.TextChannel)))

    with pytest.raises(ThreadNotFoundError, match="is not a thread"):
        await gateway.update(thread_id=500, message_id=None, name="x", content="y")


async def test_post_sends_into_the_thread() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    message_id = await gateway.post(thread_id=500, content="<@1> you are on this one")

    existing.send.assert_awaited_once_with("<@1> you are on this one")
    assert message_id == 900


async def test_a_discord_failure_surfaces_as_a_gateway_error() -> None:
    existing = thread()
    existing.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    gateway = DiscordThreadGateway(client_with(existing))

    with pytest.raises(DiscordGatewayError):
        await gateway.post(thread_id=500, content="x")


def test_long_thread_names_are_truncated() -> None:
    name = truncate_thread_name("x" * 500)

    assert len(name) == THREAD_NAME_LIMIT
    assert name.endswith("…")


def test_a_blank_thread_name_gets_a_placeholder() -> None:
    assert truncate_thread_name("   ") == "Untitled"
