"""Making Discord-authored text safe to publish on GitHub.

Issue #103, and the mirror image of `test_safe_text.py` next door. The hazards are different in
this direction: nothing here is about how a body renders, and everything is about what a body
reaches out and touches. A mention subscribes an account to the item, a reference writes a line
into another item's timeline, and an HTML comment hides whatever follows it.
"""

from __future__ import annotations

import pytest

from shannon.github.safe_text import (
    GITHUB_BODY_LIMIT,
    LINE_LIMIT,
    as_a_tag,
    as_inline_text,
    balanced,
    defuse,
    fit_body,
    one_message,
)

pytestmark = pytest.mark.unit

ZWSP = "​"

# A snowflake, which is what the tag pattern matches on.
ALICE = 111111111111111111


class TestWhatWouldNotifySomebody:
    @pytest.mark.parametrize("text", ["@octocat", "hey @octocat", "(@octocat)", "@org/team"])
    def test_a_mention_is_broken(self, text: str) -> None:
        assert "@" + ZWSP in defuse(text)

    def test_an_email_address_is_left_alone(self) -> None:
        """GitHub reads a mention only where the `@` is not preceded by a word character, so an
        address is not one. Breaking it would corrupt what somebody typed to no purpose."""
        assert defuse("mail me at alice@example.com") == "mail me at alice@example.com"

    def test_a_second_mention_on_the_same_line_is_broken_too(self) -> None:
        assert defuse("@one and @two").count(ZWSP) == 2


class TestWhatWouldTouchAnotherItem:
    @pytest.mark.parametrize("text", ["#40", "fixed by #40", "(#40)"])
    def test_a_reference_is_broken(self, text: str) -> None:
        assert "#" + ZWSP in defuse(text)

    @pytest.mark.parametrize("text", ["# Heading", "## Also a heading", "a # on its own"])
    def test_something_that_is_not_a_reference_is_left_alone(self, text: str) -> None:
        """Digits are required. A heading is cosmetic, and this rule is not about cosmetics."""
        assert defuse(text) == text

    @pytest.mark.parametrize("text", ["GH-42", "gh-42", "See GH-42"])
    def test_the_other_spelling_is_broken_too(self, text: str) -> None:
        assert "-" + ZWSP in defuse(text)


class TestWhatWouldHideTheRest:
    def test_an_html_comment_is_broken(self) -> None:
        """Unterminated, it comments out everybody else's words after it."""
        assert defuse("look <!-- at this") == "look <" + ZWSP + "!-- at this"

    def test_a_lone_angle_bracket_is_left_alone(self) -> None:
        assert defuse("a < b and c <! d") == "a < b and c <! d"

    def test_an_open_code_fence_is_closed(self) -> None:
        """Left open it swallows every line after it, including everyone else's attribution."""
        assert balanced("look\n```py\ncode") == "look\n```py\ncode\n```"

    def test_a_closed_one_is_left_exactly_as_written(self) -> None:
        text = "look\n```py\ncode\n```"

        assert balanced(text) == text

    def test_the_other_fence_character_counts_too(self) -> None:
        assert balanced("~~~").endswith("\n```")


class TestAName:
    def test_markup_in_a_display_name_cannot_restyle_the_comment(self) -> None:
        """A name goes inside `**...**`, so a name containing those would close it early and
        embolden everybody who spoke afterwards."""
        assert as_inline_text("**bob**") == r"\*\*bob\*\*"

    def test_a_backslash_is_escaped_rather_than_escaping_what_follows(self) -> None:
        assert as_inline_text("a\\b") == "a\\\\b"

    def test_a_name_that_is_a_mention_is_defused_as_well_as_escaped(self) -> None:
        """Somebody whose Discord name is `@torvalds` should not notify that account."""
        assert "@" + ZWSP in as_inline_text("@torvalds")


class TestOneMessage:
    def test_it_is_cut_before_it_is_neutralised(self) -> None:
        """The order is the whole of it. Cutting the neutralised text instead can land between an
        `@` and the zero-width space protecting it, which puts the mention back."""
        said = "x" * (LINE_LIMIT - 1) + "@octocat"

        assert "@" + ZWSP not in one_message(said), "the mention was cut off, not left half-defused"
        assert one_message(said).endswith("[...]")

    def test_a_short_message_is_not_cut(self) -> None:
        assert one_message("  hello  ") == "hello"

    def test_carriage_returns_are_folded(self) -> None:
        assert one_message("a\r\nb") == "a\nb"

    def test_a_fence_left_open_in_a_cut_message_is_still_closed(self) -> None:
        assert one_message("```py\n" + "x" * LINE_LIMIT).endswith("```")


class TestFittingTheWholeBody:
    def test_a_body_inside_the_limit_is_untouched(self) -> None:
        assert fit_body("hello") == "hello"

    def test_a_long_body_is_cut_on_a_line_boundary(self) -> None:
        body = "\n".join("x" * 100 for _ in range(1000))

        fitted = fit_body(body)

        assert len(fitted) <= GITHUB_BODY_LIMIT
        assert fitted.endswith("[...]")
        assert "x" * 99 + "x\n" in fitted, "whole lines are kept, not half of one"

    def test_a_single_line_too_long_to_cut_on_a_boundary(self) -> None:
        """No boundary to cut on, so characters are all that is left."""
        fitted = fit_body("x" * (GITHUB_BODY_LIMIT + 10))

        assert len(fitted) <= GITHUB_BODY_LIMIT
        assert fitted.endswith("[...]")

    def test_a_cut_that_lands_inside_a_code_block_still_closes_it(self) -> None:
        body = "```\n" + "\n".join("x" * 100 for _ in range(1000))

        assert fit_body(body).endswith("```")


class TestATag:
    """How somebody nobody linked is written where they were tagged. Issue #121."""

    def test_it_reads_as_a_tag_and_rings_nobody(self) -> None:
        said = as_a_tag("Alice")

        assert said == "@" + ZWSP + "Alice"
        assert "@Alice" not in said

    def test_a_name_that_is_itself_a_login_cannot_notify_whoever_holds_it(self) -> None:
        """The live bug issue #121 closes coming the other way. A display name of `@torvalds`
        used to reach GitHub intact and subscribe a stranger to the item."""
        assert "@torvalds" not in as_a_tag("@torvalds")

    def test_a_name_full_of_markup_cannot_restyle_the_line(self) -> None:
        assert as_a_tag("**bob**") == "@" + ZWSP + r"\*\*bob\*\*"


class TestATagInsideAMessage:
    """The fragment rule: what the caller supplies goes BETWEEN the pieces of what somebody typed,
    so no `defuse` ever sees a mention this bot built and no fragment can be turned into one."""

    def test_a_verified_token_becomes_what_it_was_given(self) -> None:
        said = one_message(f"hey <@{ALICE}> look", {ALICE: "@alice-gh"})

        assert said == "hey @alice-gh look"

    def test_a_typed_at_in_the_same_message_is_still_defused(self) -> None:
        """The whole point of splitting rather than substituting. One message, both answers."""
        said = one_message(f"hey <@{ALICE}>, is @octocat upstream?", {ALICE: "@alice-gh"})

        assert "@alice-gh" in said
        assert "@" + ZWSP + "octocat" in said
        assert "@octocat" not in said

    def test_a_token_nobody_answered_for_is_defused_with_the_text_round_it(self) -> None:
        said = one_message(f"hey <@{ALICE}> look")

        assert said == f"hey <@{ZWSP}{ALICE}> look"

    def test_a_token_the_cut_landed_inside_rings_nobody(self) -> None:
        """It no longer matches, so it stays in a fragment and is neutralised. That is the safe
        way for this to fail."""
        said = one_message("x" * (LINE_LIMIT - 8) + f" <@{ALICE}>", {ALICE: "@alice-gh"})

        assert "@alice-gh" not in said

    def test_a_spelling_carrying_a_backtick_cannot_open_a_fence(self) -> None:
        said = one_message(f"<@{ALICE}>", {ALICE: as_a_tag("a`b")})

        assert "```" not in said
