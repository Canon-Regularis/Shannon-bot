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
    COMMIT_MESSAGE_LIMIT,
    COMMIT_TITLE_LIMIT,
    DESCRIPTION_PREVIEW_LIMIT,
    EMPTY,
    MESSAGE_LIMIT,
    as_plain_text,
    as_prose,
    clipped,
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
        worst = quote(as_prose("*" * DESCRIPTION_PREVIEW_LIMIT), limit=DESCRIPTION_PREVIEW_LIMIT)

        assert len(worst) < MESSAGE_LIMIT

    def test_a_description_is_cut_at_its_own_limit_and_not_the_comment_one(self) -> None:
        quoted = quote("a" * 900, limit=DESCRIPTION_PREVIEW_LIMIT)

        assert len(quoted.removeprefix("> ").removesuffix("…")) == DESCRIPTION_PREVIEW_LIMIT

    def test_a_comment_still_gets_the_comment_limit_without_being_asked(self) -> None:
        """The limit is a keyword with a default, so every existing caller keeps what it had."""
        assert len(quote("a" * 900).removeprefix("> ").removesuffix("…")) == COMMENT_PREVIEW_LIMIT


class TestFlatteningMarkdownToProse:
    """What makes a description readable, which is the escaping having nothing left to escape.

    Every rule is checked with two occurrences. With one, a rule that forgot `re.MULTILINE` still
    matches at the start of the string and the test passes while every later line goes untouched.
    """

    def test_headings_lose_their_hashes(self) -> None:
        assert as_prose("## Summary\ntext\n### Detail") == "Summary\ntext\nDetail"

    def test_a_line_leading_issue_reference_keeps_its_hash(self) -> None:
        """The rule needs the space GitHub needs for a heading. Without it this reads `#3` as a
        heading, strips the hash, and the reference is gone with no way to tell it was ever there.
        """
        assert as_prose("#3 is fixed by this\n#4 as well") == "#3 is fixed by this\n#4 as well"

    def test_bullets_become_a_mark_discord_will_not_escape_back(self) -> None:
        """`-`, `*` and `+` are all markdown to Discord, so writing one of those here means the
        escaper puts the backslash straight back on and nothing has been gained."""
        flattened = as_prose("- one\n* two\n+ three")

        assert flattened == "• one\n• two\n• three"
        assert "\\" not in as_plain_text(flattened), "the escaper put the markers back"

    def test_an_indented_bullet_is_flattened_too(self) -> None:
        assert as_prose("  - one\n\t- two") == "• one\n• two"

    def test_a_blank_line_before_a_bullet_survives(self) -> None:
        r"""The rule matches spaces and tabs and not `\s`, which reaches back over the newline.
        That is the bug in Discord's own escaper that makes one bullet come out escaped and the
        next not, with a stray backslash left on the line above.
        """
        assert as_prose("intro\n\n- one\n- two") == "intro\n\n• one\n• two"

    def test_quote_markers_are_dropped_because_it_is_all_going_into_a_quote(self) -> None:
        assert as_prose("> said this\n>> and this") == "said this\nand this"

    def test_html_comments_go(self) -> None:
        """A pull request template is mostly these, and they are invisible on GitHub. Left in,
        the preview of a templated repository is the instructions rather than the description.
        """
        assert as_prose("<!-- tell us why -->real text<!-- and how -->") == "real text"

    def test_a_comment_spanning_lines_goes_too(self) -> None:
        assert as_prose("<!--\nmulti\nline\n-->kept") == "kept"

    def test_an_unterminated_comment_is_left_alone(self) -> None:
        """A greedy match would eat the rest of the description instead."""
        assert as_prose("<!-- never closed\nand the rest") == "<!-- never closed\nand the rest"

    def test_windows_line_endings_are_folded(self) -> None:
        """GitHub's web form submits CRLF, and every rule here is anchored to a line. It also
        costs a blank line two characters against the preview limit rather than one.
        """
        assert as_prose("## One\r\n\r\n\r\n\r\n- two") == "One\n\n• two"

    def test_a_run_of_blank_lines_collapses(self) -> None:
        assert as_prose("one\n\n\n\n\ntwo") == "one\n\ntwo"

    @pytest.mark.parametrize("body", ["", "   ", "\n\n\n", "## ", "<!-- only a comment -->"])
    def test_a_body_that_says_nothing_flattens_to_nothing(self, body: str) -> None:
        """What the block reads to decide there is no description to show. A body of markers is
        not empty and has nothing in it, which is why the block asks about the rendered text.
        """
        assert as_prose(body) == ""

    def test_it_never_makes_text_unsafe(self) -> None:
        """It runs before the escaping and never instead of it, and removing a comment joins
        whatever sat either side. So the thing worth proving is that the escaping still catches
        everything once this has had its turn.
        """
        hostile = "# <@1234567890>\n- @every<!-- -->one\n* **SHIPPED**\n<!-- -->`` <!-- -->`x"

        escaped = as_plain_text(as_prose(hostile))

        for live in ("<@1234567890>", "@everyone", "**SHIPPED", "```"):
            assert live not in escaped, f"{live!r} survived"
