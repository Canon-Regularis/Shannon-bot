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

from shannon.domain.enums import ObjectType
from shannon.github.mentions import MENTION_LIMIT
from shannon.github.safe_text import GITHUB_BODY_LIMIT
from shannon.services.transcripts.flush import BODY_BUDGET
from shannon.services.transcripts.lines import (
    DETAILS_OPEN_UP_TO,
    MARKER,
    NOTE,
    SUMMARY,
    TITLE,
    Relay,
    Tagged,
    TranscriptLine,
    looks_like_ours,
    not_a_transcript,
)
from shannon.services.transcripts.lines import render as render_with

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 18, 14, 2, tzinfo=UTC)
ZWSP = "​"

# Snowflakes, which is what a `<@id>` token carries.
ALICE = 111111111111111111
BOB = 222222222222222222


def line(**changes: Any) -> TranscriptLine:
    fields: dict[str, Any] = {
        "author_display_name": "alice",
        "said_at": AT,
        "content": "got the repro",
        "login": None,
        "tagged": {},
    }
    fields.update(changes)
    return TranscriptLine(**fields)


RELAY = Relay(object_type=ObjectType.PR, number=7, guild_id=1, thread_id=2)


def render(lines: list[TranscriptLine], relay: Relay = RELAY) -> str:
    """The body for these lines, with a relay nothing here is about.

    Every test below is about what the transcript says, not about which item it lands on, and
    spelling out the same four ids twenty-three times would bury that.
    """
    return render_with(relay, lines)


@dataclass
class FakeNote:
    """Enough of an `ItemNote` for the suppressor, which reads one field."""

    body: str


class TestNamingWhoSpoke:
    def test_somebody_link_knows(self) -> None:
        """Linked once, in the participants row, rather than on every line they spoke.

        The label is the name the thread showed them under, not the login: the row says who was
        in the conversation, and that is who they were in it as.
        """
        said = render([line(author_display_name="alice", login="alice-gh")])

        assert "[alice](https://github.com/alice-gh)" in said

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
        said = render([line()])

        assert TITLE in said
        assert "Discord" in said

    def test_every_line_is_in_it_in_order(self) -> None:
        said = render([line(content="first"), line(content="second")])

        assert said.index("first") < said.index("second")

    def test_what_was_said_is_neutralised(self) -> None:
        said = render([line(content="fixed by #40, thanks @octocat")])

        assert "#" + ZWSP in said
        assert "@" + ZWSP in said

    def test_the_time_is_stamped_in_utc(self) -> None:
        """The date once at the top, the clock against each message.

        A transcript is one sitting, so repeating the date on every line of it says nothing.
        """
        said = render([line()])

        assert "18 Sep 2026 · 14:02 UTC" in said
        assert "`14:02 UTC`" in said

    def test_a_message_and_its_attribution_are_separated_by_a_blank_line(self) -> None:
        """A message can begin with a list or a fence, and either needs the line to itself.

        The issue asking for this format wrote the attribution with a trailing double space
        instead. That is a line break rather than a paragraph, and a fence on the line after one
        is not at the start of a block.
        """
        assert "UTC`\n\ngot the repro" in render([line()])


class TestTheFold:
    """The thread is collapsible, and whether it opens depends on how much of it there is."""

    def test_a_short_thread_opens_expanded(self) -> None:
        said = render([line() for _ in range(DETAILS_OPEN_UP_TO)])

        assert "<details open>" in said

    def test_a_long_thread_opens_shut(self) -> None:
        """Past a certain length a transcript owns the page it is posted on, and somebody
        scrolling to the next review comment has to scroll the whole conversation."""
        said = render([line() for _ in range(DETAILS_OPEN_UP_TO + 1)])

        assert "<details open>" not in said
        assert "<details>" in said

    def test_the_summary_says_what_is_behind_it(self) -> None:
        assert f"<summary><strong>{SUMMARY}</strong></summary>" in render([line()])

    def test_a_message_cannot_climb_out_of_it(self) -> None:
        """The one hazard the fold introduces. A closing tag in something somebody typed would
        end the block early and every later message would render outside it; an opening one
        would spend the fold's own closer and leave it hanging, which GitHub shuts at the end of
        the body, swallowing the note.

        `defuse` breaks both, and this is the test that says the transcript depends on it.
        """
        said = render([line(content="</details> escaped? <details>")])

        assert said.count("</details>") == 1, "a message ended the fold"
        assert said.count("<details") == 1, "a message opened one of its own"
        assert said.endswith(NOTE), "the note fell inside the fold"


class TestWhatFramesTheThread:
    def test_it_closes_with_a_note_saying_who_posted_it(self) -> None:
        assert render([line()]).endswith(NOTE)

    def test_the_frame_survives_a_transcript_too_long_to_fit(self) -> None:
        """The case with no test before this one. `fit_body` trims whole lines from the end and
        knows only about code fences, so asked to trim the whole body it would take the closing
        tag and the note with it, leave the fold open, and have GitHub's sanitiser swallow
        everything after the thread.

        Swept rather than sampled: only a few lengths land the cut where the frame matters.
        """
        for count in range(300, 340):
            said = render([line(content="x" * 200) for _ in range(count)])

            assert len(said) <= GITHUB_BODY_LIMIT, f"{len(said)} at {count} messages"
            assert said.startswith(MARKER), "the marker went, and with it the echo suppression"
            assert said.count("</details>") == 1, "the fold lost its closer"
            assert said.endswith(NOTE), "the note fell off the end"


class TestTheBudgetTheFlusherFlushesOn:
    """`BODY_BUDGET` counts what people typed; GitHub's limit is on what that renders to.

    Nothing holds the two together but this. The frame and the per-message markup sit in the gap,
    so anything added to either eats the margin, and it would be eaten silently: the body would
    still go out, just truncated, with the end of somebody's conversation missing.
    """

    @pytest.mark.parametrize("messages", [1, 40, 200, 500])
    def test_a_full_batch_renders_inside_what_github_takes(self, messages: int) -> None:
        """Split every way a batch of that size can be, because the overhead is per message:
        the same raw budget costs more the more messages it is spread across.
        """
        each = BODY_BUDGET // messages
        said = render([line(content="x" * each) for _ in range(messages)])

        assert len(said) <= GITHUB_BODY_LIMIT, (
            f"{messages} messages of {each} render to {len(said)}, over GitHub's limit"
        )

    def test_the_frame_is_paid_for_once_rather_than_per_message(self) -> None:
        """What makes the margin hold at five hundred messages. A format charging its header to
        every line would not reach forty."""
        one = len(render([line(content="")]))
        two = len(render([line(content=""), line(content="")]))

        assert two - one < one, "the second message cost as much as the whole first body"


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


class TestWhoWasTagged:
    """Issue #121. Tagging somebody in the thread has to reach their GitHub account.

    The author's own login is a link and never an `@`, and somebody tagged is the opposite. The
    two are different questions: being recorded as having spoken is not a request to be notified,
    and tagging somebody is nothing else.
    """

    def test_a_linked_person_is_a_live_mention(self) -> None:
        said = render(
            [
                line(
                    content=f"hey <@{ALICE}> look",
                    tagged={ALICE: Tagged(display_name="Alice", login="alice-gh")},
                )
            ]
        )

        assert "@alice-gh" in said

    def test_somebody_nobody_linked_is_named_and_rings_nobody(self) -> None:
        said = render(
            [line(content=f"hey <@{ALICE}>", tagged={ALICE: Tagged(display_name="Alice")})]
        )

        assert "@" + ZWSP + "Alice" in said
        assert "@Alice" not in said

    def test_a_login_github_could_not_issue_falls_back_to_the_name(self) -> None:
        """Checked before it goes in an `@` rather than trusted, the same way it is checked
        before it goes in a URL."""
        said = render(
            [
                line(
                    content=f"hey <@{ALICE}>",
                    tagged={ALICE: Tagged(display_name="Alice", login="not a login")},
                )
            ]
        )

        assert "@not a login" not in said
        assert "@" + ZWSP + "Alice" in said

    def test_the_author_is_still_a_link_in_a_comment_that_carries_mentions(self) -> None:
        """The rule at the top of this file, restated where it could now be lost. A body that
        legitimately carries an `@` must not make the attribution line grow one."""
        said = render(
            [
                line(
                    login="alice-gh",
                    content=f"hey <@{BOB}>",
                    tagged={BOB: Tagged(display_name="Bob", login="bob-gh")},
                )
            ]
        )

        assert "[alice](https://github.com/alice-gh)" in said
        assert "[@" not in said
        assert "@bob-gh" in said

    def test_what_somebody_typed_is_still_defused_beside_a_live_one(self) -> None:
        said = render(
            [
                line(
                    content=f"<@{ALICE}> is @octocat upstream?",
                    tagged={ALICE: Tagged(display_name="Alice", login="alice-gh")},
                )
            ]
        )

        assert "@alice-gh" in said
        assert "@" + ZWSP + "octocat" in said


class TestHowManyOneCommentMayTag:
    """The same limit the comment mirror uses, for the same reason: without one a thread could
    email every linked member of the server, over and over."""

    def test_it_stops_at_the_limit(self) -> None:
        said = render(
            [
                line(
                    content=f"<@{who}>",
                    tagged={who: Tagged(display_name=f"p{who}", login=f"p{who}-gh")},
                )
                for who in range(ALICE, ALICE + MENTION_LIMIT + 4)
            ]
        )

        live = [f"p{who}-gh" for who in range(ALICE, ALICE + MENTION_LIMIT + 4)]
        assert sum(f"@{login}" in said for login in live) == MENTION_LIMIT

    def test_the_ones_past_it_are_still_named(self) -> None:
        """Named and not rung, which is what somebody who never ran `/link` already gets."""
        beyond = ALICE + MENTION_LIMIT
        said = render(
            [
                line(
                    content=f"<@{who}>",
                    tagged={who: Tagged(display_name=f"p{who}", login=f"p{who}-gh")},
                )
                for who in range(ALICE, beyond + 1)
            ]
        )

        assert f"p{beyond}" in said
        assert f"@p{beyond}-gh" not in said

    def test_one_person_across_many_messages_costs_one_place(self) -> None:
        """The budget counts distinct accounts, because a comment is what GitHub notifies from and
        a name written ten times is one notification."""
        tagged = {ALICE: Tagged(display_name="Alice", login="alice-gh")}
        said = render([line(content=f"<@{ALICE}>", tagged=tagged) for _ in range(12)])

        assert said.count("@alice-gh") == 12
