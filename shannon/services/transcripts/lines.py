"""One line of a transcript, and the comment a list of them becomes.

Pure, and ignorant of where the lines came from: the flusher builds them from captured rows, and
a later command that picks individual messages will build the same shape from something else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from shannon.domain.time import as_utc
from shannon.github.mentions import MENTION_LIMIT, is_login
from shannon.github.safe_text import as_a_tag, as_inline_text, fit_body, one_message

# What every body this feature posts opens with, and what the suppressor looks for. An HTML
# comment because GitHub renders it as nothing and hands it back verbatim on the webhook.
MARKER = "<!-- shannon-transcript -->"

HEADING = "### From the Discord thread"

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
    name = f"**{as_inline_text(line.author_display_name)}**"
    if line.login is not None and is_login(line.login):
        # The bare login as the link text, never `@login`: GitHub's mention parser reads the raw
        # markdown, so an `@` inside the brackets notifies even though the rendered link shows none.
        name = f"{name} ([{line.login}](https://github.com/{line.login}))"
    return f"{name} at {as_utc(line.said_at):%Y-%m-%d %H:%M UTC}"


def render(lines: Sequence[TranscriptLine]) -> str:
    """The comment body carrying these lines, in the order they were said.

    A blank line between the attribution and what was said, rather than a trailing double space:
    a message can begin with a list or a fence, and either needs the line to itself.
    """
    live: set[str] = set()
    blocks = [MARKER, HEADING]
    blocks.extend(
        f"{_said_by(line)}\n\n{one_message(line.content, _tags(line, live))}" for line in lines
    )
    return fit_body("\n\n".join(blocks))


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
