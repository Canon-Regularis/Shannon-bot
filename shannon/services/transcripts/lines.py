"""One line of a transcript, and the comment a list of them becomes.

Issue #103. Pure, and deliberately ignorant of where the lines came from: the flusher produces
them from captured rows, and a later command that picks individual messages will produce the same
shape from something else. That is the seam, and this module is the whole of it.

The marker is the other half of the feature. A comment posted here comes straight back as an
`issue_comment` delivery, and the mirror that puts GitHub comments into Discord threads would put
this one into the very thread it was transcribed from. Nothing else in this project has that
problem: every other command writes to GitHub and relies on the echo to say what happened.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from shannon.domain.time import as_utc
from shannon.github.safe_text import as_inline_text, fit_body, one_message

# What every body this feature posts opens with, and what the suppressor looks for. An HTML
# comment because GitHub renders it as nothing and hands it back verbatim on the webhook, which
# is not a guess: `discord_bot/safe_text.py` strips these on the way in precisely because pull
# request templates arrive carrying them.
MARKER = "<!-- shannon-transcript -->"

HEADING = "### From the Discord thread"

# GitHub's own rule for a login: alphanumerics and single hyphens, 39 characters at most. Checked
# before one is put in a URL rather than trusted, because a login that does not match would break
# out of the link and take the rest of the line with it. A row that fails this renders as the
# plain display name, which is what an unlinked person gets anyway.
_LOGIN = re.compile(r"\A[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}\Z")


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One captured message, with whatever is known about who said it."""

    author_display_name: str
    said_at: datetime
    content: str
    # The GitHub account `/link` knows this person by, where it knows one. Rendered as a link
    # rather than an `@login`, so reading a transcript does not subscribe everybody named in it
    # to the item.
    login: str | None = None


def looks_like_ours(body: str) -> bool:
    """Whether a comment body is a transcript this bot posted.

    Anchored to the start, which is what makes it safe against a quote reply. GitHub's quote
    button copies a body verbatim and prefixes every line with `> `, so a reply quoting a
    transcript carries the marker but not at the front, and is mirrored like any other comment.

    Leading whitespace is allowed and nothing else is.
    """
    return body.lstrip().startswith(MARKER)


class HasABody(Protocol):
    """The one field the suppressor reads.

    Narrower than the `ItemNote` the mirror declares, deliberately. A predicate handed to
    `worth_posting` may ask for less than the mirror passes and still satisfy it, and saying what
    it actually looks at is both more honest and what lets a test stand a plain body in its place.
    """

    body: str


def not_a_transcript(note: HasABody) -> bool:
    """Whether a comment arriving from GitHub is somebody else's rather than one of ours.

    What the comments mirror is given, and the whole of the echo suppression. Every other command
    in this project writes to GitHub and RELIES on the delivery coming back, because the echo is
    what puts the line in the thread. This one is the opposite: the comment it posts was made out
    of that thread, so mirroring it back would put the conversation into the thread it came from,
    under this bot's name, a minute after it happened.

    Given to the mirror rather than to the parser, and that matters. The branch this feeds takes no
    claim, so a later decision to stop declining these would replay every one of them instead of
    finding them all recorded as mirrored.
    """
    return not looks_like_ours(note.body)


def _said_by(line: TranscriptLine) -> str:
    name = f"**{as_inline_text(line.author_display_name)}**"
    if line.login is not None and _LOGIN.match(line.login):
        # The bare login as the link text, never `@login`. GitHub's mention parser reads the raw
        # markdown, so an `@` inside the brackets notifies that account even though the rendered
        # link shows no mention at all.
        name = f"{name} ([{line.login}](https://github.com/{line.login}))"
    return f"{name} at {as_utc(line.said_at):%Y-%m-%d %H:%M UTC}"


def render(lines: Sequence[TranscriptLine]) -> str:
    """The comment body carrying these lines, in the order they were said.

    A blank line between the attribution and what was said, rather than a trailing double space.
    A message can begin with a list or a fence, and either of those needs the line to itself.
    """
    blocks = [MARKER, HEADING]
    blocks.extend(f"{_said_by(line)}\n\n{one_message(line.content)}" for line in lines)
    return fit_body("\n\n".join(blocks))
