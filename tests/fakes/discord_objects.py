from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import discord


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **_: Any) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_: Any) -> None:
        self.messages.append(content)


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **_: Any) -> None:
        self.messages.append(content)


@dataclass
class FakeRole:
    name: str
    id: int = 0


@dataclass
class FakeGuildPermissions:
    administrator: bool = False


@dataclass
class FakeMember:
    id: int = 1
    name: str = "tester"
    roles: list[FakeRole] = field(default_factory=list)
    guild_permissions: FakeGuildPermissions = field(default_factory=FakeGuildPermissions)
    global_name: str | None = None
    nick: str | None = None

    def __str__(self) -> str:
        return self.name


class FakeInteraction:
    """Enough of discord.Interaction for command callbacks to run without Discord."""

    def __init__(
        self,
        *,
        guild_id: int | None = 1,
        channel_id: int | None = 10,
        user: FakeMember | None = None,
        channel: object | None = None,
        app_permissions: discord.Permissions | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = user or FakeMember()
        # What this bot may do in the channel the command was run in. Defaults to the permission
        # list the README asks for, which grants nothing beyond reading and writing threads, so a
        # command that depends on more finds that out here rather than in front of a user.
        self.app_permissions = app_permissions or discord.Permissions(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            create_public_threads=True,
            manage_threads=True,
            read_message_history=True,
        )
        # A real text channel by default, because that is where a command normally runs and
        # /register refuses anywhere threads cannot be opened.
        self.channel = channel if channel is not None else MagicMock(spec=discord.TextChannel)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    @property
    def replies(self) -> list[str]:
        return self.response.messages + self.followup.messages

    @property
    def reply(self) -> str:
        assert len(self.replies) == 1, f"expected one reply, got {self.replies}"
        return self.replies[0]
