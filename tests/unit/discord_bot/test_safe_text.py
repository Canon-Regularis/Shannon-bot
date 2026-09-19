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

from shannon.discord_bot.rich_text import as_rich_text
from shannon.discord_bot.safe_text import (
    COMMENT_PREVIEW_LIMIT,
    COMMIT_MESSAGE_LIMIT,
    COMMIT_TITLE_LIMIT,
    DESCRIPTION_PREVIEW_LIMIT,
    EMPTY,
    MESSAGE_LIMIT,
    as_plain_text,
    clipped,
    code_span,
    cut,
    fit,
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


def test_the_commit_message_limit_is_the_number_that_was_asked_for() -> None:
    """Written out rather than compared against anything, because there is nothing to compare it
    to: it is a product decision and the issue that asked for commit lines named it."""
    assert COMMIT_MESSAGE_LIMIT == 250


def test_a_commit_title_is_cut_shorter_than_the_message_under_it() -> None:
    """Not a taste. `fit` drops whole lines, so a subject longer than what follows it would take
    the statistics line down with it and the thread would show half a title and no numbers."""
    assert 0 < COMMIT_TITLE_LIMIT < COMMIT_MESSAGE_LIMIT
    assert COMMIT_TITLE_LIMIT == 120


class TestClippingGitHubText:
    """The cut `quote` was doing inline, now shared with the commit lines.

    It is the ORDER that is being pinned here. Cut first, escape second: the other way round
    looks identical on ordinary text and leaves a stray backslash the moment the cut lands on
    one, un-escaping whatever came after it.
    """

    def test_text_exactly_at_the_limit_is_left_whole(self) -> None:
        assert clipped("a" * 40, limit=40) == "a" * 40

    def test_one_character_over_is_cut(self) -> None:
        assert clipped("a" * 41, limit=40) == "a" * 40 + "…"

    def test_nothing_but_whitespace_clips_to_nothing(self) -> None:
        """Answered as empty rather than as a blank line, because callers read it as a section to
        leave out."""
        assert clipped("   \n  \n ", limit=40) == ""

    def test_the_cut_falls_on_the_raw_text_rather_than_the_escaped_text(self) -> None:
        """Forty underscores escape to eighty characters. Cutting the escaped text would keep
        twenty of them and a trailing backslash; cutting the raw text keeps all forty, escaped."""
        assert clipped("_" * 40, limit=40) == "\\_" * 40

    def test_a_cut_never_leaves_a_backslash_with_nothing_to_protect(self) -> None:
        """The failure this ordering exists to prevent. The character at the limit is one that
        gets escaped, so a cut after escaping would sit between the backslash and the asterisk."""
        clipped_text = clipped("a" * 39 + "*rest", limit=40)

        assert not clipped_text.removesuffix("…").endswith("\\")

    def test_a_mention_inside_clipped_text_reaches_nobody(self) -> None:
        """Whatever this renders goes into a thread with no allow-list beside it, so a commit
        message carrying a literal mention would ring whoever that id belongs to."""
        assert "<@123456>" not in clipped("ping <@123456> please", limit=100)


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


class TestCuttingABody:
    """What somebody wrote on GitHub, made safe and made short.

    It stopped being a blockquote in issue #113, so what is left is the cut and the escaping.
    The order of those two is the whole of it and is checked below either side of the limit.
    """

    def test_a_body_at_the_preview_limit_is_kept_whole(self) -> None:
        body = "a" * COMMENT_PREVIEW_LIMIT

        cut = clipped(body, limit=COMMENT_PREVIEW_LIMIT)

        assert "…" not in cut
        assert cut == body

    def test_one_character_over_is_cut_and_marked(self) -> None:
        cut = clipped("a" * (COMMENT_PREVIEW_LIMIT + 1), limit=COMMENT_PREVIEW_LIMIT)

        assert cut.endswith("…")
        assert len(cut.removesuffix("…")) == COMMENT_PREVIEW_LIMIT

    def test_the_lines_of_a_body_are_left_as_written(self) -> None:
        """Nothing is put on the front of them any more, blank ones included."""
        cut = clipped("first\nsecond\n\nfourth", limit=COMMENT_PREVIEW_LIMIT)

        assert cut.split("\n") == ["first", "second", "", "fourth"]

    @pytest.mark.parametrize("body", ["", "   ", "\n\n", None])
    def test_a_body_with_nothing_in_it_cuts_to_nothing(self, body: str | None) -> None:
        """An empty block is a heading with a blank under it, which reads as a mistake."""
        assert clipped(body, limit=COMMENT_PREVIEW_LIMIT) == ""

    def test_the_cut_happens_before_the_escaping_and_not_after(self) -> None:
        """Escaping only adds characters, so cutting after it would cut inside a backslash pair.

        Cutting first is also what lets the preview end anywhere: whatever markup the cut lands
        in the middle of is neutralised on the way out rather than left half open.
        """
        body = "*" * (COMMENT_PREVIEW_LIMIT + 100)

        cut = clipped(body, limit=COMMENT_PREVIEW_LIMIT)

        assert "\\*" in cut, "the body reached Discord unescaped"
        assert cut.count("*") == COMMENT_PREVIEW_LIMIT, "it cut after escaping, not before"


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


class TestTheDescriptionLimit:
    """Pinned against the number rather than against the comment limit it happens to equal.

    The two are different things and either may move. Asserting one against the other is how a
    change to the comment preview silently becomes a change to the description as well.
    """

    def test_it_is_the_number_it_is(self) -> None:
        assert DESCRIPTION_PREVIEW_LIMIT == 700

    def test_a_description_at_its_worst_still_leaves_room_for_the_block_around_it(self) -> None:
        """Escaping can double the length, every line gains two characters for the quote, and the
        block it goes under has ten fields of its own. A limit that fits on its own and not in a
        block would be a description that silently disappears from a busy item.
        """
        worst = as_rich_text("*" * DESCRIPTION_PREVIEW_LIMIT).text

        assert len(worst) < MESSAGE_LIMIT

    def test_a_description_is_cut_at_its_own_limit_and_not_the_comment_one(self) -> None:
        cut = clipped("a" * 900, limit=DESCRIPTION_PREVIEW_LIMIT)

        assert len(cut.removesuffix("…")) == DESCRIPTION_PREVIEW_LIMIT


class TestCuttingWithoutMakingSafe:
    """The length half of `clipped`, lifted out so the one order that works exists once.

    The RAW text is cut and only then neutralised, so a cut can never land between a backslash or
    a zero-width space and the character it was protecting. `clipped` and `rich_text` both take
    this cut and then neutralise differently, which is the whole reason it is its own function.
    """

    def test_text_inside_the_limit_is_left_alone(self) -> None:
        assert cut("a" * 40, limit=40) == "a" * 40

    def test_one_character_over_is_cut_and_marked(self) -> None:
        assert cut("a" * 41, limit=40) == "a" * 40 + "…"

    def test_nothing_is_escaped_on_the_way(self) -> None:
        """The difference from `clipped`, said out loud: this one only shortens."""
        assert cut("**bold**", limit=40) == "**bold**"

    @pytest.mark.parametrize("body", ["", "   ", "\n\n", None])
    def test_nothing_but_whitespace_cuts_to_nothing(self, body: str | None) -> None:
        assert cut(body, limit=40) == ""
