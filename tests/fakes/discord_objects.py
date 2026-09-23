from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import discord
from discord import Message, MessageType, ui

from shannon.discord_bot import layout
from shannon.discord_bot.responses import OWED, REFUSED, SUCCEEDED


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
    """What an interaction was answered with, and whether anybody else could see it.

    `ephemeral` defaults to False here rather than to this project's True, and the difference
    matters: discord.py's own default is False, so a call that forgets the keyword sends a public
    message. Defaulting to True would record what the caller meant instead of what Discord would
    have done, which is the one thing a stand-in must never do.
    """

    def __init__(self) -> None:
        self.deferred = False
        self.deferred_ephemerally = False
        self.messages: list[str] = []
        self.ephemerally: list[bool] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, *, ephemeral: bool = False, **_: Any) -> None:
        self.deferred = True
        self.deferred_ephemerally = ephemeral

    async def send_message(
        self,
        content: str | None = None,
        *,
        view: object = None,
        ephemeral: bool = False,
        **_: Any,
    ) -> None:
        self.messages.append(_said(content, view))
        self.ephemerally.append(ephemeral)


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.ephemerally: list[bool] = []

    async def send(
        self,
        content: str | None = None,
        *,
        view: object = None,
        ephemeral: bool = False,
        **_: Any,
    ) -> None:
        self.messages.append(_said(content, view))
        self.ephemerally.append(ephemeral)


# The three an outcome can carry, so a reply with none is told from one with an unexpected
# first word rather than having its first word eaten.
_MARKS = frozenset({SUCCEEDED, OWED, REFUSED})


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
    live connection state, so the stand-in is structural and this is where that is admitted.

    Nothing checks that the shape below still matches, which is worth saying rather than leaving
    somebody to assume otherwise. `test_stand_ins_match_what_they_replace` compares a fake
    against a Protocol by reading `__protocol_attrs__` off it, and `discord.Message` is a
    concrete class with no such attribute. Nor would this pass it: the fields below are only the
    ones `capture` reads, and being narrower than the real thing is the very failure that table
    exists to catch. There is no narrow Protocol to put on the other side either, because
    `on_message` overrides discord.py's own signature and `capture` is deliberately the one
    module in the project that touches `discord.Message` at all.

    So a field discord.py renames shows up as `test_capture` or `test_client` failing, and not
    as a conformance failure naming the field.
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
    def ephemerally(self) -> list[bool]:
        """Whether each reply was private, in the order `replies` gives them.

        Kept as a second list rather than folded into `replies`, which around a hundred tests read
        as plain strings. Until issue #144 nothing in this suite could see this at all: the keyword
        landed in `**_` and was thrown away, so a change that made every command reply public would
        have passed the whole suite without a murmur.
        """
        return self.response.ephemerally + self.followup.ephemerally

    @property
    def reply(self) -> str:
        assert len(self.replies) == 1, f"expected one reply, got {self.replies}"
        return self.replies[0]

    @property
    def mark(self) -> str:
        """Which of the three outcome marks the one reply opens with. Issue #147.

        Empty for the one reply that carries none: `/mentions` reading back what it found did
        not change anything, and a tick on the answer to a question would be claiming it had.
        """
        head = self.reply.split(" ", 1)[0]
        return head if head in _MARKS else ""

    @property
    def said(self) -> str:
        """The one reply with its outcome mark taken off.

        A wording assertion is about the sentence. Every reply now opens with one of three marks,
        and repeating those three characters in a hundred assertions would say nothing that
        `mark` does not say once, on its own, where it is the thing under test.
        """
        return self.reply[len(self.mark) :].lstrip() if self.mark else self.reply
