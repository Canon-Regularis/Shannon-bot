"""Deciding whether a Discord message belongs in a transcript, and reducing it to plain values.

Issue #103. This is the only place in the project that touches `discord.Message`, and it exists so
that stays true: the services take `CapturedMessage` and know nothing about discord.py, the same
way `ThreadGateway` keeps it out of everything that writes.

Every rule here is a skip rather than a transform. What is captured is what somebody typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import discord

from shannon.db.models import DISPLAY_NAME_WIDTH, TRANSCRIPT_LINE_WIDTH

# The two kinds that are somebody talking. Everything else Discord calls a message is the system
# narrating: "X started a thread", a pin, a channel rename, a join. Transcribing those would put
# Discord's own furniture into a GitHub comment.
SAID_BY_A_PERSON = frozenset({discord.MessageType.default, discord.MessageType.reply})


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    """One message worth keeping, as plain values."""

    thread_id: int
    message_id: int
    author_id: int
    author_display_name: str
    content: str
    said_at: datetime


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
    """
    return bool(message.clean_content.strip())


def captured(message: discord.Message) -> CapturedMessage:
    """Reduce a message to what a transcript needs.

    `clean_content` rather than `content`, because the raw form carries `<@123>` and `<#456>`,
    which are ids rather than anything a reader of a GitHub comment could use. discord.py builds
    the names it substitutes from the event payload, so this resolves without the members intent.

    What it produces is still not safe to publish: it turns `<@123>` into `@DisplayName`, which is
    a live GitHub mention when that name happens to match a login. `github.safe_text` is what
    closes that, at the point the comment is rendered.
    """
    return CapturedMessage(
        thread_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        author_display_name=message.author.display_name[:DISPLAY_NAME_WIDTH],
        content=message.clean_content.strip()[:TRANSCRIPT_LINE_WIDTH],
        said_at=message.created_at,
    )
