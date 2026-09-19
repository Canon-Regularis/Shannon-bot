"""What a transcript comment looks like, and how one is told apart from anybody else's comment.

Issue #103. The marker is the whole of the echo suppression, and the two tests that matter most
here are the ones about what it does NOT match: everything else in this project relies on GitHub
sending a write straight back, and getting this wrong either duplicates a conversation into the
thread it came from or silences comments real people wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from shannon.services.transcripts.lines import (
    HEADING,
    MARKER,
    TranscriptLine,
    looks_like_ours,
    not_a_transcript,
    render,
)

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 18, 14, 2, tzinfo=UTC)
ZWSP = "​"


def line(**changes: Any) -> TranscriptLine:
    fields: dict[str, Any] = {
        "author_display_name": "alice",
        "said_at": AT,
        "content": "got the repro",
        "login": None,
    }
    fields.update(changes)
    return TranscriptLine(**fields)


@dataclass
class FakeNote:
    """Enough of an `ItemNote` for the suppressor, which reads one field."""

    body: str


class TestNamingWhoSpoke:
    def test_somebody_link_knows(self) -> None:
        said = render([line(login="alice-gh")])

        assert "[alice-gh](https://github.com/alice-gh)" in said

    def test_the_link_text_is_never_an_at_mention(self) -> None:
        """GitHub's mention parser reads the raw markdown, so an `@` inside the brackets notifies
        that account even though the rendered link shows no mention at all. The user asked for a
        link precisely so reading a transcript does not subscribe everybody named in it."""
        assert "[@" not in render([line(login="alice-gh")])

    def test_somebody_it_does_not(self) -> None:
        said = render([line(login=None)])

        assert "alice" in said
        assert "github.com" not in said

    def test_a_login_that_could_break_out_of_the_url_is_not_used(self) -> None:
        """A login is checked against GitHub's own rule before it goes in a URL rather than
        trusted, because one that does not match would end the link early and take the rest of the
        line with it."""
        said = render([line(login="not) a [login")])

        assert "github.com" not in said

    def test_a_display_name_that_is_itself_a_mention(self) -> None:
        said = render([line(author_display_name="@torvalds")])

        assert "@" + ZWSP in said

    def test_a_display_name_carrying_markup_cannot_restyle_the_comment(self) -> None:
        said = render([line(author_display_name="**bob**")])

        assert r"\*\*bob\*\*" in said


class TestTheComment:
    def test_it_opens_with_the_marker(self) -> None:
        """First characters of the body, which is what makes the suppressor safe to anchor."""
        assert render([line()]).startswith(MARKER)

    def test_it_says_where_it_came_from(self) -> None:
        assert HEADING in render([line()])

    def test_every_line_is_in_it_in_order(self) -> None:
        said = render([line(content="first"), line(content="second")])

        assert said.index("first") < said.index("second")

    def test_what_was_said_is_neutralised(self) -> None:
        said = render([line(content="fixed by #40, thanks @octocat")])

        assert "#" + ZWSP in said
        assert "@" + ZWSP in said

    def test_the_time_is_stamped_in_utc(self) -> None:
        assert "2026-09-18 14:02 UTC" in render([line()])

    def test_a_message_and_its_attribution_are_separated_by_a_blank_line(self) -> None:
        """A message can begin with a list or a fence, and either needs the line to itself."""
        assert "UTC\n\ngot the repro" in render([line()])


class TestTellingOneOfOursApart:
    def test_a_transcript_is_recognised(self) -> None:
        assert looks_like_ours(render([line()])) is True

    def test_an_ordinary_comment_is_not(self) -> None:
        assert looks_like_ours("I think the cache is the problem") is False

    def test_a_quote_reply_to_a_transcript_is_not(self) -> None:
        """The sharp one. GitHub's quote button copies a body verbatim and prefixes every line
        with `> `, so an anchored match is what stops somebody replying to a transcript having
        their reply silently dropped instead of mirrored."""
        quoted = "\n".join(f"> {said}" for said in render([line()]).split("\n"))

        assert looks_like_ours(quoted) is False
        assert MARKER in quoted, "the marker really is in there, just not at the front"

    def test_leading_whitespace_is_allowed(self) -> None:
        assert looks_like_ours("\n  " + MARKER + "\nrest") is True

    def test_the_mirror_posts_anybody_elses_comment(self) -> None:
        assert not_a_transcript(FakeNote(body="I think the cache is the problem")) is True

    def test_and_declines_one_of_ours(self) -> None:
        assert not_a_transcript(FakeNote(body=render([line()]))) is False
