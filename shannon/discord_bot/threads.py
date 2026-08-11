from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    ThreadNotFoundError,
)

logger = logging.getLogger(__name__)

# Discord's own ceilings.
THREAD_NAME_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ThreadHandle:
    thread_id: int
    message_id: int | None = None


class ThreadGateway(Protocol):
    """Everything the sync path does to Discord.

    The sync service depends on this rather than on discord.py, which is what lets it be
    tested without a gateway connection.
    """

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle: ...

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle: ...

    async def post(self, *, thread_id: int, content: str) -> int | None: ...


def truncate_thread_name(name: str) -> str:
    name = name.strip() or "Untitled"
    if len(name) <= THREAD_NAME_LIMIT:
        return name
    return name[: THREAD_NAME_LIMIT - 1] + "…"


class DiscordThreadGateway:
    """ThreadGateway on top of a live discord.py client.

    Handles both text channels and forum channels, because a server may keep pull requests in
    either and the requirements name both.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        channel = await self._channel(channel_id)
        name = truncate_thread_name(name)

        try:
            if isinstance(channel, discord.ForumChannel):
                created = await channel.create_thread(name=name, content=content)
                return ThreadHandle(thread_id=created.thread.id, message_id=created.message.id)

            if isinstance(channel, discord.TextChannel):
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread
                )
                message = await thread.send(content)
                return ThreadHandle(thread_id=thread.id, message_id=message.id)
        except discord.HTTPException as exc:
            raise DiscordGatewayError(f"Discord refused to create a thread: {exc}") from exc

        raise ChannelNotFoundError(
            f"Channel {channel_id} is a {type(channel).__name__}, which cannot hold threads"
        )

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle:
        thread = await self._thread(thread_id)
        name = truncate_thread_name(name)

        try:
            # Renames are rate limited hard, so only spend one when the title actually moved.
            if thread.name != name:
                await thread.edit(name=name)

            resolved_message_id = await self._edit_or_post(thread, message_id, content)
        except discord.HTTPException as exc:
            raise DiscordGatewayError(f"Discord refused to update the thread: {exc}") from exc

        return ThreadHandle(thread_id=thread.id, message_id=resolved_message_id)

    async def post(self, *, thread_id: int, content: str) -> int | None:
        thread = await self._thread(thread_id)
        try:
            message = await thread.send(content)
        except discord.HTTPException as exc:
            raise DiscordGatewayError(f"Discord refused to post to the thread: {exc}") from exc
        return message.id

    async def _edit_or_post(
        self, thread: discord.Thread, message_id: int | None, content: str
    ) -> int:
        if message_id is not None:
            try:
                message = await thread.fetch_message(message_id)
            except discord.NotFound:
                # Someone deleted the metadata message. Post a fresh one and adopt its ID.
                logger.info("metadata message %s is gone, posting a replacement", message_id)
            else:
                await message.edit(content=content)
                return message.id

        replacement = await thread.send(content)
        return replacement.id

    async def _channel(self, channel_id: int) -> discord.abc.GuildChannel:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ChannelNotFoundError(f"Channel {channel_id} is not reachable") from exc
        return channel  # type: ignore[return-value]

    async def _thread(self, thread_id: int) -> discord.Thread:
        channel = self._client.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ThreadNotFoundError(f"Thread {thread_id} is not reachable") from exc

        if not isinstance(channel, discord.Thread):
            raise ThreadNotFoundError(f"{thread_id} is not a thread")
        return channel
