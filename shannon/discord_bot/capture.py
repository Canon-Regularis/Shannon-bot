"""Deciding whether a Discord message belongs in a transcript, and reducing it to plain values.

Issue #103. This is the only place in the project that touches `discord.Message`, and it exists so
that stays true: the services take `CapturedMessage` and know nothing about discord.py, the same
way `ThreadGateway` keeps it out of everything that writes.

Every rule here is a skip but one, and the one is `_said` below. It is discord.py's own
transform, reimplemented because discord.py's version throws away the thing issue #121 needs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import discord

from shannon.db.models import DISPLAY_NAME_WIDTH, TRANSCRIPT_LINE_WIDTH

# The two kinds that are somebody talking. Everything else Discord calls a message is the system
# narrating: "X started a thread", a pin, a channel rename, a join. Transcribing those would put
# Discord's own furniture into a GitHub comment.
SAID_BY_A_PERSON = frozenset({discord.MessageType.default, discord.MessageType.reply})

# Discord's syntax for the three things a message can point at, and the same pattern
# `clean_content` reads in discord.py's `message.py`. Fifteen to twenty digits is a snowflake.
_POINTS_AT = re.compile(r"<(@[!&]?|#)([0-9]{15,20})>")

# What discord.py writes for something it cannot resolve, kept word for word so a thread reads
# the way it did before issue #121.
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
    # Who this message tagged, by Discord id, with the name each had when it was said. No
    # default, so nothing can build one of these and quietly leave out the half issue #121
    # turns on.
    mentions: Mapping[int, str]


def from_a_person(message: discord.Message) -> bool:
    """Whether somebody said this, as opposed to a bot or Discord itself.

    The bot check is doing more work than it looks. This bot's own mirrored GitHub comments live
    in the very threads being captured, so without it every comment arriving from GitHub would be
    transcribed straight back to GitHub, and each round would carry the last one with it.

    Separate statements rather than one condition, so each reason stands on its own and a test can
    prove it by itself.
    """
    if message.author.bot:
        return False
    # Surer than the flag above for something posted through a webhook, which carries a synthetic
    # author that does not always read as a bot.
    if message.webhook_id is not None:
        return False
    return message.type in SAID_BY_A_PERSON


def has_words(message: discord.Message) -> bool:
    """Whether there is any text to write down.

    False for an attachment on its own, a sticker on its own and a poll, all of which arrive
    carrying nothing. It is also what a message content intent granted in name only looks like,
    which is why the caller says so once rather than passing over it quietly: a botched grant and
    a thread where people only post pictures are identical from here.

    Asked of the raw content, which is where an unpaid intent actually shows up and which is
    what `_said` reads. The two agreed about emptiness when this asked `clean_content`, so it
    is one fewer attribute to stand in for rather than a change of behaviour.
    """
    return bool(message.content.strip())


def _said(message: discord.Message) -> tuple[str, dict[int, str]]:
    """What was typed, with every tag of a person kept as an id, and who those people are.

    `clean_content` in all but one respect, and that respect is the whole of issue #121: it turns
    `<@123>` into `@DisplayName` and throws the id away. The id is the only thing `/link` knows
    somebody by, so by the time a comment is rendered there was nothing left to look up.

    **`message.mentions` is the authority and the text is only a pointer into it.** Discord builds
    that list itself, off the payload, so an id written in the content that Discord did not read as
    a mention is not one and reads as `@deleted-user`, exactly as it does now. That is also what
    makes the token pointless to forge: typing `<@123>` IS mentioning 123, and it rings them here
    as well as there, so there is nothing to be had by typing it rather than clicking a name. The
    equivalence holds only for Discord's own syntax, which is why nothing invented goes into the
    token: a shape Discord does not parse would ping on GitHub having pinged nobody here.

    Roles and channels resolve the way `clean_content` resolves them and are deliberately
    unchanged, because issue #121 is about people. `get_channel_or_thread` is the public twin of
    the `_resolve_channel` discord.py reaches for. discord.py's `role_mentions` fallback is dropped
    rather than copied: that list is built by calling `get_role` on each mentioned id, so it can
    hold nothing `get_role` does not.

    Its closing `escape_mentions` is dropped too. That defuses `@everyone` for Discord, and nothing
    sends this text back to Discord; `github.safe_text` covers the one direction it travels.
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
        # A thread being logged is always in a server. Answered rather than asserted, because the
        # type says this can be None and a transcript is not worth raising over in the handler
        # that runs for every message in every server.
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

    What this produces is still not safe to publish. `github.safe_text` is what closes that, at the
    point the comment is rendered, and since issue #121 it is also what decides which of the tags
    kept here becomes a live GitHub mention.

    The map is built from the substitutions `_said` actually made rather than from
    `message.mentions` wholesale, and that is not tidiness. A reply carries the author it answers
    in `mentions` with no token anywhere in the text, so taking that list whole would tag somebody
    on GitHub for every reply in a thread.
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
