"""`code_span`, `lines_within` and `LINK`, pinned where they are defined. #270.

Their callers in `discord_bot/` and `github/` test them only in passing: one checks that no
three-backtick fence appears, none checks the fence, the padding or the zero-width split itself.
So a tidy-up could change any of them and every test would still pass. These hold the exact
output before `domain/` is refactored, and after.
"""

from __future__ import annotations

import pytest

from shannon.domain.text import LINK, ZERO_WIDTH_SPACE, code_span, lines_within


class TestACodeSpanItsOwnBackticksCannotBreak:
    def test_plain_text_takes_a_single_backtick_fence(self) -> None:
        assert code_span("bug") == "`bug`"

    def test_one_backtick_inside_takes_a_two_backtick_fence(self) -> None:
        assert code_span("a`b") == "``a`b``"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("`x", "`` `x ``"), ("x`", "`` x` ``")],
        ids=["leading", "trailing"],
    )
    def test_a_backtick_at_either_end_is_padded_off_the_fence(
        self, text: str, expected: str
    ) -> None:
        """Without the space the backtick would merge with the fence and lengthen it."""
        assert code_span(text) == expected

    def test_a_run_of_backticks_is_split_with_a_zero_width_space(self) -> None:
        """Discord reads three backticks as a code block, so the fence never grows past two."""
        assert code_span("a``b") == f"``a`{ZERO_WIDTH_SPACE}`b``"

    def test_a_long_run_is_split_between_every_backtick(self) -> None:
        split = ZERO_WIDTH_SPACE.join("```")
        assert code_span("a```b") == f"``a{split}b``"


class TestTheWholeLinesWithinABudget:
    """Only inside its contract: the caller has already found the text too long for the budget."""

    def test_a_budget_filled_exactly_keeps_the_last_line(self) -> None:
        assert lines_within("ab\ncd\nef", 5) == ["ab", "cd"]

    def test_a_budget_one_short_leaves_the_last_line_out(self) -> None:
        """`ab` costs 2 and `cd` costs 3, its newline counted, so 4 is one short of both."""
        assert lines_within("ab\ncd\nef", 4) == ["ab"]

    def test_a_budget_between_lines_keeps_only_the_lines_that_fit(self) -> None:
        assert lines_within("ab\ncd\nef", 6) == ["ab", "cd"]

    def test_the_first_line_pays_for_no_newline(self) -> None:
        assert lines_within("ab\ncd\nef", 2) == ["ab"]

    def test_a_first_line_over_the_budget_gives_nothing(self) -> None:
        """Never a cut mid-line, which could land inside `**bold**` or a mention."""
        assert lines_within("abcdef\ngh", 3) == []

    def test_an_empty_line_costs_its_newline(self) -> None:
        assert lines_within("ab\n\ncd", 3) == ["ab", ""]


class TestAMaskedLink:
    def test_a_markdown_link_is_matched_with_its_label_and_address(self) -> None:
        found = LINK.search("see [the docs](https://example.com/a) here")

        assert found is not None
        assert found.group(0) == "[the docs](https://example.com/a)"
        assert found.groups() == ("the docs", "https://example.com/a")

    def test_two_links_on_one_line_are_two_matches(self) -> None:
        """discord.py's own pattern is greedy and would run from the first to the last."""
        text = "[a](https://a.example) and [b](https://b.example)"

        assert [found.group(0) for found in LINK.finditer(text)] == [
            "[a](https://a.example)",
            "[b](https://b.example)",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "[a](https://example.com/a b)",
            "[a](<@123>)",
            "[a](https://example.com/<@123>)",
            "[a\nb](https://example.com)",
            "[a](https://example.com\n)",
            "[a](https://example.com/(b))",
            "[a[b]](https://example.com)",
        ],
        ids=[
            "space in the address",
            "mention as the address",
            "angle bracket in the address",
            "newline in the label",
            "newline in the address",
            "parenthesis in the address",
            "bracket in the label",
        ],
    )
    def test_the_shapes_it_refuses_are_not_links(self, text: str) -> None:
        assert LINK.search(text) is None
