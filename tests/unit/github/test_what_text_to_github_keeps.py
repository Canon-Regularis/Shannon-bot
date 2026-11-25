"""What Discord-authored text becomes on its way into a GitHub comment, pinned for #271.

Characterisation, before #271 touches `github/safe_text` and moves the tag token's definition.
Two kinds of thing are pinned here. What `defuse` and `fit_body` answer for the inputs whose
answer is staying, exactly, so a reshaped pattern cannot drift on them. And the tag token, which
is a contract between two layers that nothing else joins: `discord_bot.capture` WRITES it and
stores it in a transcript row, and `github.safe_text` READS it back when the transcript is
rendered. Each side has its own tests against a literal `<@id>`, so either could change shape and
the other's tests would stay green while every tagged person in a transcript went quiet. The test
at the bottom drives the real producer's output into the real consumer, which is the only kind of
test that would notice.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.capture import captured
from shannon.github.safe_text import defuse, fit_body, one_message
from tests.fakes.discord_objects import FakeMentioned, a_message

ZWSP = "​"

# Snowflakes: Discord ids are fifteen to twenty digits, and both sides of the token match on that.
ALICE = 111111111111111111
BOB = 222222222222222222
LARGEST = 2**64 - 1


class TestWhatDefuseBreaks:
    @pytest.mark.parametrize(
        ("typed", "published"),
        [
            ("@octocat", f"@{ZWSP}octocat"),
            ("ask @octocat please", f"ask @{ZWSP}octocat please"),
            ("@acme/security", f"@{ZWSP}acme/security"),
            ("#12", f"#{ZWSP}12"),
            ("see #12.", f"see #{ZWSP}12."),
            ("GH-12", f"GH-{ZWSP}12"),
            ("(GH-12)", f"(GH-{ZWSP}12)"),
        ],
    )
    def test_a_mention_or_a_reference_is_broken_with_a_zero_width_space(
        self, typed: str, published: str
    ) -> None:
        assert defuse(typed) == published

    def test_inside_a_code_span_it_is_broken_all_the_same(self) -> None:
        """Nothing here parses markdown, so a code span is text like any other. The backticks
        themselves are left alone, so the span still renders as one."""
        assert defuse("`@octocat #12`") == f"`@{ZWSP}octocat #{ZWSP}12`"


class TestWhatDefuseLeavesAsWritten:
    @pytest.mark.parametrize(
        "typed",
        [
            "&#123;",
            "a &#123; b",
            "https://github.com/acme/widget/issues/40",
            "https://github.com/acme/widget/pull/7#issuecomment-99",
            "someone@example.com",
            "# Heading",
            "#",
            "issue#",
            "GH-",
            "a < b and <3",
        ],
    )
    def test_text_that_reaches_nothing_is_untouched(self, typed: str) -> None:
        """A numeric character reference is markup, not a reference, and a full URL is the
        deliberate exception the `defuse` docstring gives."""
        assert defuse(typed) == typed


class TestFittingABody:
    def test_a_body_inside_the_limit_is_returned_as_written_open_fence_and_all(self) -> None:
        body = "```\nnever closed"

        assert fit_body(body, limit=len(body)) == body

    def test_a_long_body_is_cut_after_the_last_whole_line_that_fits(self) -> None:
        """Budget 22 less the six of the marker and the four held for a closing fence: twelve,
        which holds two four-letter lines and the newline between them, and not a third."""
        body = "aaaa\nbbbb\ncccc\ndddd\neeee"

        assert fit_body(body, limit=22) == "aaaa\nbbbb\n[...]"

    def test_a_cut_inside_a_fence_closes_it_after_the_marker(self) -> None:
        body = "intro\n```\ncode1\ncode2\ncode3\n```"

        assert fit_body(body, limit=30) == "intro\n```\ncode1\n[...]\n```"

    def test_a_cut_after_a_closed_fence_adds_no_closer(self) -> None:
        body = "```\nab\n```\nmore\nmore\nmore"

        assert fit_body(body, limit=24) == "```\nab\n```\n[...]"

    def test_the_cut_body_fits_the_limit_it_was_given(self) -> None:
        body = "\n".join(f"line {n}" for n in range(1000))

        for limit in (40, 41, 42, 43, 500):
            assert len(fit_body(body, limit=limit)) <= limit


class TestTheTagTokenFromCaptureToTheComment:
    """The producer's real output, fed to the real consumer. Issue #271 (and #269 before it)."""

    def test_a_tag_capture_keeps_becomes_the_linked_persons_mention(self) -> None:
        said = captured(
            a_message(
                content=f"thanks <@{ALICE}>, can you look?",
                mentions=[FakeMentioned(id=ALICE, display_name="Alice")],
            )
        )

        published = one_message(said.content, {ALICE: "@octocat"})

        assert published == "thanks @octocat, can you look?"

    def test_the_nickname_form_reaches_the_comment_as_the_same_mention(self) -> None:
        """Discord writes `<@!id>` for a member with a nickname; capture stores one shape."""
        said = captured(
            a_message(
                content=f"<@!{ALICE}> and <@{BOB}>",
                mentions=[FakeMentioned(id=ALICE), FakeMentioned(id=BOB, display_name="Bob")],
            )
        )

        published = one_message(said.content, {ALICE: "@octocat", BOB: "@hubot"})

        assert published == "@octocat and @hubot"

    def test_a_tag_of_somebody_with_no_spelling_is_left_inert(self) -> None:
        said = captured(
            a_message(content=f"hey <@{BOB}>", mentions=[FakeMentioned(id=BOB, display_name="B")])
        )

        assert one_message(said.content, {}) == f"hey <@{ZWSP}{BOB}>"

    def test_the_largest_id_discord_can_issue_survives_the_trip(self) -> None:
        said = captured(a_message(content=f"<@{LARGEST}>", mentions=[FakeMentioned(id=LARGEST)]))

        assert one_message(said.content, {LARGEST: "@octocat"}) == "@octocat"

    def test_what_capture_keeps_is_the_map_the_render_is_keyed_by(self) -> None:
        """The render looks a token up by the int capture parsed out of it, so the two have to be
        the same number, not merely the same digits."""
        said = captured(
            a_message(content=f"<@!{ALICE}>", mentions=[FakeMentioned(id=ALICE, display_name="A")])
        )

        assert dict(said.mentions) == {ALICE: "A"}
        spelled = {found: f"@{name.lower()}" for found, name in said.mentions.items()}
        assert one_message(said.content, spelled) == "@a"

    def test_a_typed_token_discord_did_not_read_as_a_mention_never_goes_live(self) -> None:
        """Not in `message.mentions`, so capture writes Discord's own placeholder and there is no
        token left for the render to turn into anybody."""
        said = captured(a_message(content=f"hey <@{ALICE}>", mentions=[]))

        assert one_message(said.content, {ALICE: "@octocat"}) == f"hey @{ZWSP}deleted-user"
