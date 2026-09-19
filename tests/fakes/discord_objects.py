from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import discord
from discord import Message, MessageType, ui

from shannon.discord_bot import layout


def _said(content: str | None, view: object) -> str:
    """What the person in front of the bot reads, whichever shape it arrived in.

    A reply is either a string or a card, and a card is components with no content at all. The
    words are the same either way, which is the law `layout` is written to keep, so this reads
    them back out of the view rather than making every existing assertion know which it got.

    It walks the real view rather than trusting it, which is the same bargain the thread fake
    makes: the adapter is the riskiest new code in the project and a stand-in that skipped it
    would leave it unexecuted by the whole command tier.
    """
    if view is None:
        return content or ""
    assert content is None, "a message cannot carry both components and content"
    return "\n".join(layout.words(cast(ui.LayoutView, view)))


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **_: Any) -> None:
        self.deferred = True

    async def send_message(
        self, content: str | None = None, *, view: object = None, **_: Any
    ) -> None:
        self.messages.append(_said(content, view))


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str | None = None, *, view: object = None, **_: Any) -> None:
        self.messages.append(_said(content, view))


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


@dataclass
class FakeAuthor:
    """Who said something, as `capture` reads it. Issue #103."""

    id: int = 77
    display_name: str = "alice"
    bot: bool = False


@dataclass
class FakeChannel:
    id: int = 9001


@dataclass
class FakeMentioned:
    """Somebody a message tagged, as `capture` reads them off `message.mentions`. Issue #121."""

    id: int = 111111111111111111
    display_name: str = "Alice"


@dataclass
class FakeNamed:
    """A role or a channel, which capture asks the guild for by id and reads the name of."""

    id: int
    name: str


@dataclass
class FakeGuild:
    """The two lookups capture makes for a role and a channel mention.

    Positional-only, because discord.py declares both that way and a stand-in that took them by
    keyword would accept calls the real thing refuses.
    """

    roles: dict[int, FakeNamed] = field(default_factory=dict)
    channels: dict[int, FakeNamed] = field(default_factory=dict)

    def get_role(self, role_id: int, /) -> FakeNamed | None:
        return self.roles.get(role_id)

    def get_channel_or_thread(self, channel_id: int, /) -> FakeNamed | None:
        return self.channels.get(channel_id)


@dataclass
class FakeMessage:
    """Enough of discord.Message for the capture rules to run without a gateway.

    `content` rather than `clean_content` since issue #121. Capture does discord.py's own
    substitution itself now, because `clean_content` turns `<@123>` into a display name and throws
    away the id, which is the only thing `/link` knows anybody by.

    `mentions` is what makes a tag a tag. Capture treats it as the authority and the text as a
    pointer into it, so a `<@id>` here that is not in this list is not a mention.
    """

    id: int = 501
    content: str = "hello"
    mentions: list[FakeMentioned] = field(default_factory=list)
    guild: FakeGuild | None = field(default_factory=FakeGuild)
    author: FakeAuthor = field(default_factory=FakeAuthor)
    channel: FakeChannel = field(default_factory=FakeChannel)
    webhook_id: int | None = None
    type: MessageType = MessageType.default
    created_at: datetime = datetime(2026, 9, 18, 14, 2, tzinfo=UTC)


def a_message(**changes: Any) -> Message:
    """A `FakeMessage` handed over as the `discord.Message` the capture rules are typed against.

    One cast, here, rather than one at every call site. A real `Message` cannot be built without a
    live connection state, so the stand-in is structural and this is where that is admitted. The
    test that holds every stand-in against what it replaces is what stops the shape drifting.
    """
    return cast(Message, FakeMessage(**changes))  # pyright: ignore[reportArgumentType]


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
