"""The boundaries of what Discord will accept, pinned against the numbers rather than the code.

Everything here was found by changing an operator and watching the whole unit tier pass anyway.
Nine of the eighteen logic changes possible in `safe_text` went unnoticed, including inverting the
comment preview cut and moving Discord's own message limit by one, and the reason is the same
every time: what covers this module is the block formatters and the property tests, and those
assert `len(rendered) <= MESSAGE_LIMIT`, comparing the output against the very constant that
decides it. Raise the constant and the assertion rises with it.

So the constants are checked against the numbers Discord documents, written out here, and the two
functions are checked one character either side of every limit they enforce. What is wrong when
one of these is wrong is not subtle: Discord refuses the whole message, and the thread stops
saying anything at all.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.safe_text import (
    COMMENT_PREVIEW_LIMIT,
    EMPTY,
    MESSAGE_LIMIT,
    as_plain_text,
    code_span,
    fit,
    quote,
)

# Discord's own limit on the content of a message, from its documentation rather than from the
# module under test. One over and the API refuses the message outright.
DISCORD_MESSAGE_LIMIT = 2000

# Spelled out rather than imported, for the same reason the limit above is.
NEWLINE = "\n"
TRUNCATED = NEWLINE + "…"


def test_the_message_limit_is_the_one_discord_enforces() -> None:
    """Everything else here is measured against this, so it cannot measure itself."""
    assert MESSAGE_LIMIT == DISCORD_MESSAGE_LIMIT


def test_the_preview_limit_leaves_room_for_a_block_around_it() -> None:
    """A comment is a pointer to the discussion, not a copy, and it is quoted inside a block
    carrying a header and a link. Escaping only ever makes the body longer."""
    assert 0 < COMMENT_PREVIEW_LIMIT < MESSAGE_LIMIT


class TestFittingAMessage:
    """`fit` is the last thing between a rendered block and Discord refusing it."""

    def test_a_message_exactly_at_the_limit_is_left_alone(self) -> None:
        message = "x" * MESSAGE_LIMIT

        assert fit(message) == message

    def test_one_character_over_is_brought_back_under(self) -> None:
        assert len(fit("x" * (MESSAGE_LIMIT + 1))) <= MESSAGE_LIMIT

    # Fifty characters and a newline each, so forty lines fit and forty-one do not.
    @pytest.mark.parametrize("lines", [41, 100, 400])
    def test_it_cuts_on_a_line_boundary_and_says_it_did(self, lines: int) -> None:
        """Each line is built balanced, so dropping whole lines leaves the rest rendering.

        Cutting mid-line can land inside `**bold**` or halfway through a mention, and the rest of
        the message goes with it.
        """
        message = "\n".join("y" * 50 for _ in range(lines))

        fitted = fit(message)

        assert len(fitted) <= MESSAGE_LIMIT
        assert fitted.endswith("…")
        kept = fitted.removesuffix("\n…")
        assert all(line == "y" * 50 for line in kept.split("\n")), "it cut inside a line"

    def test_it_keeps_every_line_that_fits_and_not_one_fewer(self) -> None:
        """A budget out by one throws away a line that would have fitted.

        At the bottom of a metadata block that is a whole field nobody sees, and which field it
        is depends on how long the title happened to be, so it looks like nothing rather than
        like a bug. This is what pins the arithmetic rather than the outcome: the outcome fits
        either way.
        """
        line = "y" * 50
        message = NEWLINE.join(line for _ in range(400))

        kept = fit(message).removesuffix(TRUNCATED).split(NEWLINE)

        assert len(NEWLINE.join(kept)) + len(TRUNCATED) <= MESSAGE_LIMIT, "one line too many"
        assert len(NEWLINE.join([*kept, line])) + len(TRUNCATED) > MESSAGE_LIMIT, "one would fit"

    def test_a_line_that_exactly_fills_the_budget_is_kept(self) -> None:
        """The one place a single character decides anything, built to land on it.

        With lines of the same length the budget can never be hit exactly, which is why three
        different one-character changes to this arithmetic left every other test here passing.
        A block of uneven lines is what real metadata is, and this is that: a run of long ones
        followed by one that fits the remainder to the character.
        """
        budget = MESSAGE_LIMIT - len(TRUNCATED)
        long_lines = ["y" * 50] * 39
        used = 39 * 50 + 38
        exactly_the_rest = "z" * (budget - used - 1)

        kept = fit(NEWLINE.join([*long_lines, exactly_the_rest, "tail" * 20]))

        assert kept.removesuffix(TRUNCATED).endswith(exactly_the_rest), "the last line that fits"
        assert len(kept) == MESSAGE_LIMIT

    def test_a_single_line_too_long_to_cut_is_still_brought_under(self) -> None:
        """No boundary to cut on, so this is the one case that cuts anywhere."""
        fitted = fit("z" * (MESSAGE_LIMIT * 2))

        assert len(fitted) == MESSAGE_LIMIT

    def test_the_marker_it_appends_is_counted_in_the_budget(self) -> None:
        """The whole point of the budget: a message trimmed to exactly the limit and then given
        a marker is a message one over the limit."""
        for length in (MESSAGE_LIMIT + 1, MESSAGE_LIMIT + 2, MESSAGE_LIMIT * 3):
            assert len(fit("\n".join("w" * 40 for _ in range(length // 41)))) <= MESSAGE_LIMIT


class TestQuotingABody:
    """What somebody wrote on GitHub, made safe and made short."""

    def test_a_body_at_the_preview_limit_is_quoted_whole(self) -> None:
        body = "a" * COMMENT_PREVIEW_LIMIT

        quoted = quote(body)

        assert "…" not in quoted
        assert quoted == f"> {body}"

    def test_one_character_over_is_cut_and_marked(self) -> None:
        quoted = quote("a" * (COMMENT_PREVIEW_LIMIT + 1))

        assert quoted.endswith("…")
        assert len(quoted.removeprefix("> ").removesuffix("…")) == COMMENT_PREVIEW_LIMIT

    def test_every_line_of_a_body_is_quoted(self) -> None:
        """A body whose second line escaped the block would render as ordinary message text."""
        quoted = quote("first\nsecond\n\nfourth")

        assert quoted.split("\n") == ["> first", "> second", ">", "> fourth"]

    @pytest.mark.parametrize("body", ["", "   ", "\n\n", None])
    def test_a_body_with_nothing_in_it_quotes_to_nothing(self, body: str | None) -> None:
        """An empty block is a header with a stray `>` under it, which reads as a mistake."""
        assert quote(body) == ""

    def test_the_cut_happens_before_the_escaping_and_not_after(self) -> None:
        """Escaping only adds characters, so cutting after it would cut inside a backslash pair.

        Cutting first is also what lets the preview end anywhere: whatever markup the cut lands
        in the middle of is neutralised on the way out rather than left half open.
        """
        body = "*" * (COMMENT_PREVIEW_LIMIT + 100)

        quoted = quote(body)

        assert "\\*" in quoted, "the body reached Discord unescaped"
        assert quoted.count("*") == COMMENT_PREVIEW_LIMIT, "it cut after escaping, not before"


class TestTheOtherTwoThingsThisModuleDecides:
    def test_a_label_carrying_backticks_cannot_open_a_code_block(self) -> None:
        """Three backticks are a block in Discord rather than a longer inline span, so a fence
        that grows past two turns one line of the metadata into a block."""
        for backticks in range(1, 6):
            spanned = code_span("bug" + "`" * backticks)

            assert "```" not in spanned, f"{backticks} backticks opened a block"

    def test_the_word_for_nothing_is_a_word_and_not_an_empty_string(self) -> None:
        """A field rendered blank reads as a bug in the bot rather than as an empty field."""
        assert EMPTY.strip() != ""

    def test_escaped_text_is_never_shorter_than_what_went_in(self) -> None:
        """Every budget here is worked out before escaping, so escaping shrinking a string would
        make all of them wrong in the direction that matters."""
        for text in ("plain", "**bold**", "a_b_c", "`code`", "<@1234>", "@everyone"):
            assert len(as_plain_text(text)) >= len(text)
