"""One line of a transcript, and the comment a list of them becomes.

Pure, and ignorant of where the lines came from: the flusher builds them from captured rows, and
a later command that picks individual messages will build the same shape from something else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from shannon.domain.enums import ObjectType
from shannon.domain.time import as_utc
from shannon.github import markdown
from shannon.github.mentions import MENTION_LIMIT, is_login
from shannon.github.safe_text import (
    GITHUB_BODY_LIMIT,
    as_a_tag,
    as_inline_text,
    fit_body,
    one_message,
)

# What every body this feature posts opens with, and what the suppressor looks for. An HTML
# comment because GitHub renders it as nothing and hands it back verbatim on the webhook.
MARKER = "<!-- shannon-transcript -->"

TITLE = "## SHANNON // DISCORD RELAY"

SUMMARY = "View thread"

NOTE = "**Shannon** mirrored this thread from Discord."

# The label on the link back. Fixed words rather than the thread name, which nothing on this
# path knows: the item carries a title, and  does not copy it onto what it answers with.
OPEN_IN_DISCORD = "Open in Discord"

# Up to this many, the thread reads inline. Past it a transcript would own the page it is posted
# on, and somebody scrolling to the next review comment has to scroll the whole conversation.
DETAILS_OPEN_UP_TO = 10

# What each kind of item is called where the transcript names it.
_CALLED = {ObjectType.PR: "PR", ObjectType.ISSUE: "Issue", ObjectType.TICKET: "Card"}

# A login is checked with `is_login` before it goes in a URL or an `@`: one that is not a login
# would break out of the link and take the rest of the line with it.


@dataclass(frozen=True, slots=True)
class Tagged:
    """Somebody a captured message tagged, and whatever is known about them."""

    display_name: str
    # The account `/link` knows them by, rendered as a live `@login` unlike the author's below:
    # tagging somebody is a request to notify them.
    login: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One captured message, with whatever is known about who said it."""

    author_display_name: str
    said_at: datetime
    content: str
    # The account `/link` knows them by, rendered as a link rather than an `@login`: being
    # recorded as having spoken is not a request to be notified.
    login: str | None = None
    # Keyed by Discord id: that is what the `<@123>` in the content points at.
    tagged: Mapping[int, Tagged] = field(default_factory=dict[int, Tagged])


@dataclass(frozen=True, slots=True)
class Relay:
    """Where a transcript came from, and the item it is going onto.

    Built by whoever publishes rather than carried on the lines: a line knows who said it and
    when, and nothing about which pull request it is bound for.
    """

    object_type: ObjectType
    number: int
    guild_id: int
    thread_id: int

    @property
    def item(self) -> str:
        return f"{_CALLED[self.object_type]} #{self.number}"

    @property
    def thread_url(self) -> str:
        """The link back to the conversation this came from.

        Two ids rather than a channel name: both are already on the row, neither goes stale when
        somebody renames a channel, and neither is text anybody typed, so nothing here can break
        out of the link.
        """
        return f"https://discord.com/channels/{self.guild_id}/{self.thread_id}"


def looks_like_ours(body: str) -> bool:
    """Whether a comment body is a transcript this bot posted.

    Anchored to the start, which is what makes it safe against a quote reply: GitHub's quote
    button prefixes every copied line with `> `, so the marker is no longer at the front.
    """
    return body.lstrip().startswith(MARKER)


class HasABody(Protocol):
    """The one field the suppressor reads.

    Narrower than the `ItemNote` the mirror passes, which is all `worth_posting` requires.
    Read-only, for the reason `ItemNote` itself gives.
    """

    @property
    def body(self) -> str: ...


def not_a_transcript(note: HasABody) -> bool:
    """Whether a comment arriving from GitHub is somebody else's rather than one of ours.

    Mirroring our own transcript back puts the conversation into the thread it came from. Applied
    in the mirror, not the parser, so these stay unclaimed and all replay if the rule is dropped.
    """
    return not looks_like_ours(note.body)


def _said_by(line: TranscriptLine) -> str:
    """Who said one message, and when.

    The clock only, because the date is in the header and a transcript is one sitting. No link
    here either: the participants row carries those once rather than on every line somebody
    spoke, which on a long thread is the same link forty times.
    """
    return f"**{as_inline_text(line.author_display_name)}** · `{as_utc(line.said_at):%H:%M UTC}`"


def _profile(line: TranscriptLine) -> str:
    """One speaker's name, linked to the account `/link` knows them by if there is one.

    The display name as the label, not the login: this row says who was in the conversation, and
    they were in it under the name the thread showed. `as_inline_text` is what keeps a name that
    is itself a mention from notifying whoever holds it, since GitHub's mention parser reads the
    raw markdown and would see an `@` inside the brackets the rendered link does not show.
    """
    name = as_inline_text(line.author_display_name)
    if line.login is None or not is_login(line.login):
        return name
    return markdown.link(name, f"https://github.com/{line.login}")


def _participants(lines: Sequence[TranscriptLine]) -> str:
    """Everyone who spoke, in the order they first did.

    Names rather than `@login`, for the reason `TranscriptLine.login` gives: being recorded as
    having spoken is not a request to be notified.
    """
    spoke: dict[tuple[str, str | None], str] = {}
    for line in lines:
        spoke.setdefault((line.author_display_name, line.login), _profile(line))
    return ", ".join(spoke.values())


def _header(relay: Relay, lines: Sequence[TranscriptLine]) -> str:
    """Everything above the fold: what this is, where it came from, and how much of it there is.

    The stamp is the last message rather than the moment of posting. A reader wants to know when
    the conversation happened, and the two are minutes apart anyway because a transcript goes out
    once the thread has been quiet for a while.
    """
    last = as_utc(lines[-1].said_at)
    return "\n\n".join(
        [
            f"{MARKER}\n{TITLE}",
            f"> **Thread synchronised**\n> `{relay.item}` · `{last:%d %b %Y · %H:%M UTC}`",
            markdown.table(
                [
                    ("Source", "Discord"),
                    ("Thread", markdown.link(OPEN_IN_DISCORD, relay.thread_url)),
                    ("Participants", _participants(lines)),
                    ("Messages", f"`{len(lines)}`"),
                ]
            ),
        ]
    )


def render(relay: Relay, lines: Sequence[TranscriptLine]) -> str:
    """The comment body carrying these lines, in the order they were said.

    A blank line between the attribution and what was said, rather than a trailing double space:
    a message can begin with a list or a fence, and either needs the line to itself.

    The frame is measured and its room reserved, so a thread too long to fit loses messages from
    the end rather than the tag that closes the fold. `fit_body` trims whole lines and knows only
    about code fences; asked to trim the whole body it would leave the fold hanging open and take
    the note with it, and GitHub's sanitiser would then swallow everything after the thread.
    """
    # The flusher drops a batch whose messages were all deleted before it went out, so there is
    # no such thing as a transcript of nothing to render.
    assert lines, "a transcript with no lines is never published"

    expanded = len(lines) <= DETAILS_OPEN_UP_TO
    head = _header(relay, lines)
    foot = "\n\n".join(["---", markdown.note(NOTE)])
    frame = "\n\n".join([head, markdown.details(SUMMARY, "", expanded=expanded), foot])

    live: set[str] = set()
    said = "\n\n".join(
        f"{_said_by(line)}\n\n{one_message(line.content, _tags(line, live))}" for line in lines
    )
    fitted = fit_body(said, limit=GITHUB_BODY_LIMIT - len(frame))
    return "\n\n".join([head, markdown.details(SUMMARY, fitted, expanded=expanded), foot])


def _tags(line: TranscriptLine, live: set[str]) -> dict[int, str]:
    """How each person this message tagged is spelled, by Discord id.

    Swapped in by id rather than searched for in the text, where what somebody typed could be
    read as a name. One mention budget for the whole comment rather than per message, because
    GitHub notifies from the comment.
    """
    spelled: dict[int, str] = {}
    for discord_user_id, person in line.tagged.items():
        login = person.login
        if login is not None and is_login(login) and (login in live or len(live) < MENTION_LIMIT):
            live.add(login)
            spelled[discord_user_id] = f"@{login}"
        else:
            spelled[discord_user_id] = as_a_tag(person.display_name)
    return spelled
