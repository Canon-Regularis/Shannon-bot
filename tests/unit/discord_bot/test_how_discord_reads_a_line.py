"""The test-side spelling of how Discord reads a heading and its small print. Issue #245.

`tests/support/discord_markdown.py` is what the heading tests ask "would a reader see a heading
here?", so it is pinned here to the behaviour of Discord's own rules - the cases below are the ones
that make Discord differ from GitHub, and from a naive reading of either.
"""

from __future__ import annotations

import pytest

from tests.support.discord_markdown import headings_drawn, subtext_anywhere

pytestmark = pytest.mark.unit

ZWSP = "\N{ZERO WIDTH SPACE}"
NBSP = "\N{NO-BREAK SPACE}"


class TestWhatDiscordDrawsAsAHeading:
    @pytest.mark.parametrize(
        ("text", "drawn"),
        [
            ("# Big", [(1, "Big")]),
            ("## Medium", [(2, "Medium")]),
            ("### Small", [(3, "Small")]),
            ("## Title ##", [(2, "Title")]),
            ("# C#", [(1, "C")]),
            (f"# C#{ZWSP}", [(1, f"C#{ZWSP}")]),
            ("#\tTabbed", [(1, "Tabbed")]),
            (f"#{NBSP}nbsp", [(1, "nbsp")]),
            ("#\N{ZERO WIDTH NO-BREAK SPACE}x", [(1, "x")]),
            ("#\nnext", [(1, "next")]),
            ("  ## x", [(2, "x")]),
            ("> ## Quoted", [(2, "Quoted")]),
            ("one\n## two\n### three", [(2, "two"), (3, "three")]),
            ("# \\## x", [(1, "\\## x")]),
            ("\t## x", [(2, "x")]),
            ("one\r## x", [(2, "x")]),
            ("intro\n>>> ## x", [(2, "x")]),
            ("# a\n  ## b", [(1, "a"), (2, "b")]),
        ],
        ids=[
            "one hash",
            "two",
            "three",
            "a closing run",
            "a hash at the end of the words",
            "a hash kept by a zero-width space",
            "a tab",
            "a no-break space",
            "a byte-order mark",
            "whitespace running into the next line",
            "indented at the very start",
            "in a quote",
            "after a line of text",
            "words opening on an escaped hash",
            "a tab in front, which Discord makes four spaces",
            "after a carriage return, which Discord makes a line break",
            "in a quote of the rest of the message",
            "indented under a heading, which takes its own line break",
        ],
    )
    def test_it_is_drawn(self, text: str, drawn: list[tuple[int, str]]) -> None:
        assert headings_drawn(text) == drawn

    @pytest.mark.parametrize(
        "text",
        [
            "#### Four",
            "#hashtag",
            "#3 is fixed",
            "\\# escaped",
            "# ## nested",
            "#\x1cx",
            "a\N{LINE SEPARATOR}# b",
            "text\n  ## indented under text",
            "- ## in a list",
            "1. ## in a numbered list",
            "> > ## in a nested quote",
            ">## no space after the quote",
            "```\n# in code\n```",
            "x ```\n# after a fence opened mid-line",
            f"{ZWSP}# behind a zero-width space",
            "#",
        ],
        ids=[
            "four hashes",
            "no space",
            "an issue reference",
            "escaped",
            "words opening on hashes",
            "a separator Discord does not call whitespace",
            "a line separator Discord does not break on",
            "indented under a line of text",
            "a list item",
            "a numbered list item",
            "a nested quote",
            "a quote with no space",
            "code",
            "code opened mid-line",
            "a zero-width space in front",
            "nothing at all after it",
        ],
    )
    def test_it_is_not(self, text: str) -> None:
        assert headings_drawn(text) == []


class TestWhatDiscordReadsAsSmallPrint:
    @pytest.mark.parametrize(
        "text",
        [
            "-# small",
            "  -# indented",
            f"{NBSP}-# behind a no-break space",
            "> -# quoted",
            "- -# listed",
            "```\n-#include <a.h>\n```",
            "-\f# fed",
        ],
    )
    def test_it_is_read(self, text: str) -> None:
        assert subtext_anywhere(text) != []

    @pytest.mark.parametrize(
        "text",
        [f"-{ZWSP}# broken", "a -# mid-line", "\\-# escaped", "#- the other way round"],
    )
    def test_it_is_not(self, text: str) -> None:
        assert subtext_anywhere(text) == []
