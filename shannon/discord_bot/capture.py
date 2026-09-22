"""Deciding whether a Discord message belongs in a transcript, and reducing it to plain values.

The only place in the project that touches `discord.Message`: the services take
`CapturedMessage` and know nothing about discord.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import discord

from shannon.db.models import DISPLAY_NAME_WIDTH, TRANSCRIPT_LINE_WIDTH

# The two kinds that are somebody talking. Every other Discord message type is the system
# narrating: "X started a thread", a pin, a channel rename, a join.
SAID_BY_A_PERSON = frozenset({discord.MessageType.default, discord.MessageType.reply})

# Discord's mention syntax, the pattern `clean_content` reads in discord.py's `message.py`.
# Fifteen to twenty digits is a snowflake.
_POINTS_AT = re.compile(r"<(@[!&]?|#)([0-9]{15,20})>")

# What discord.py writes for something it cannot resolve, kept word for word.
DELETED_USER = "@deleted-user"
DELETED_ROLE = "@deleted-role"
DELETED_CHANNEL = "#deleted-channel"


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    """One message worth keeping, as plain values."""

    thread_id: int
    message_id: int
    author_id: int
    author_display_name: str
    content: str
    said_at: datetime
    # Who this message tagged, by Discord id, with the name each had when it was said.
    mentions: Mapping[int, str]


def from_a_person(message: discord.Message) -> bool:
    """Whether somebody said this, as opposed to a bot or Discord itself.

    Without the bot check, this bot's own mirrored GitHub comments in the threads being captured
    would be transcribed straight back to GitHub, each round carrying the last one with it.
    """
    if message.author.bot:
        return False
    # A webhook post carries a synthetic author that does not always read as a bot.
    if message.webhook_id is not None:
        return False
    return message.type in SAID_BY_A_PERSON


def has_words(message: discord.Message) -> bool:
    """Whether there is any text to write down.

    False for an attachment, a sticker or a poll on its own, all of which arrive carrying
    nothing. A message content intent granted in name only is indistinguishable from here, which
    is why the caller says so once. Asked of the raw content, where an unpaid intent shows up.
    """
    return bool(message.content.strip())


def _said(message: discord.Message) -> tuple[str, dict[int, str]]:
    """What was typed, with every tag of a person kept as an id, and who those people are.

    `clean_content` in all but one respect: it turns `<@123>` into `@DisplayName` and throws the
    id away, and the id is the only thing `/link` knows somebody by.

    `message.mentions` is the authority and the text is only a pointer into it, so an id written
    in the content that Discord did not read as a mention resolves to `@deleted-user`. The token
    cannot be forged: typing `<@123>` is mentioning 123, and it rings them here as well as on
    GitHub. Only Discord's own syntax goes into the token, because a shape Discord does not parse
    would ping on GitHub having pinged nobody here.

    Roles and channels resolve as `clean_content` resolves them, through the public twin of the
    `_resolve_channel` discord.py reaches for. Its `role_mentions` fallback is dropped because
    that list is built by calling `get_role` on each mentioned id, so it can hold nothing
    `get_role` does not. Its closing `escape_mentions` is dropped too: that defuses `@everyone`
    for Discord, and nothing sends this text back to Discord.
    """
    tagged = {member.id: member for member in message.mentions}
    named: dict[int, str] = {}
    guild = message.guild

    def swap(match: re.Match[str]) -> str:
        kind, found = match.group(1), int(match.group(2))
        if kind in ("@", "@!"):
            member = tagged.get(found)
            if member is None:
                return DELETED_USER
            named[found] = member.display_name[:DISPLAY_NAME_WIDTH]
            # Normalised to one shape, so the render has one token to look for rather than two.
            return f"<@{found}>"
        # A thread being logged is always in a server; the type says otherwise, and a
        # transcript is not worth raising over in the handler that runs for every message.
        if guild is None:
            return DELETED_ROLE if kind == "@&" else DELETED_CHANNEL
        if kind == "@&":
            role = guild.get_role(found)
            return f"@{role.name}" if role else DELETED_ROLE
        channel = guild.get_channel_or_thread(found)
        return f"#{channel.name}" if channel else DELETED_CHANNEL

    return _POINTS_AT.sub(swap, message.content), named


def captured(message: discord.Message) -> CapturedMessage:
    """Reduce a message to what a transcript needs.

    The output is not safe to publish; `github.safe_text` closes that when the comment is
    rendered, and also decides which of the tags kept here becomes a live GitHub mention. The map
    holds only the substitutions `_said` made: a reply carries the author it answers in
    `mentions` with no token in the text, so the whole list would tag somebody for every reply.
    """
    content, named = _said(message)
    return CapturedMessage(
        thread_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        author_display_name=message.author.display_name[:DISPLAY_NAME_WIDTH],
        content=content.strip()[:TRANSCRIPT_LINE_WIDTH],
        said_at=message.created_at,
        mentions=named,
    )
