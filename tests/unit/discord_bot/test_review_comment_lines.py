"""One inline review comment, as a line in a thread.

Issue #107. The comment body goes through the same quoting every other note does, so what is worth
pinning here is the half above it: which file and which line, said in a way that survives a path
somebody chose to be awkward about.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import format_review_comment
from shannon.discord_bot.safe_text import MESSAGE_LIMIT, REVIEW_PATH_LIMIT, clipped_path
from shannon.domain.models import Actor, RepositorySnapshot, ReviewCommentSnapshot

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)

COMMENT = ReviewCommentSnapshot(
    repository=REPO,
    item_number=7,
    comment_id=1,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#discussion_r1",
    body="This claim wants giving back on cancellation too.",
    path="shannon/services/notes.py",
    line=205,
    author=Actor("monalisa", 200),
    created_at=datetime(2026, 8, 11, 10, 30, tzinfo=UTC),
)


def said(snapshot: ReviewCommentSnapshot, **kwargs: object) -> str:
    return format_review_comment(snapshot, **kwargs).splitlines()[0]


class TestWhereTheCommentIs:
    def test_a_single_line(self) -> None:
        assert said(COMMENT).startswith(
            "**monalisa** commented on `shannon/services/notes.py` L205"
        )

    def test_a_range(self) -> None:
        assert "L200-205" in said(replace(COMMENT, start_line=200))

    def test_a_whole_file_names_no_line(self) -> None:
        """A file-level comment empties both the current line and the original one, which is what
        tells it apart from one the diff has moved under."""
        line = said(replace(COMMENT, line=None))

        assert "`shannon/services/notes.py`" in line
        assert "L205" not in line
        assert "outdated" not in line

    def test_an_outdated_comment_says_so(self) -> None:
        """The number points at where the comment was written, not at where the file is now.
        Saying which is the difference between a stale pointer and a wrong one."""
        assert "L205 (outdated)" in said(replace(COMMENT, line=None, original_line=205))

    def test_a_comment_with_no_path_says_nothing_about_a_file(self) -> None:
        assert said(replace(COMMENT, path="")) == "**monalisa** commented <t:1786444200:f>"


class TestWhoLeftIt:
    def test_a_reply_is_told_apart_from_a_first_comment(self) -> None:
        """Two people talking on one line reads as two unrelated comments otherwise."""
        assert said(replace(COMMENT, in_reply_to_id=98765)).startswith("**monalisa** replied on")

    def test_a_linked_account_is_a_mention(self) -> None:
        assert said(COMMENT, mentions={"monalisa": 4242}).startswith("**<@4242>** commented")

    def test_an_unlinked_account_is_named_in_plain_text(self) -> None:
        assert said(COMMENT).startswith("**monalisa**")


class TestWhatItCarries:
    def test_the_body_is_quoted(self) -> None:
        assert "> This claim wants giving back on cancellation too." in format_review_comment(
            COMMENT
        )

    def test_the_link_back_is_the_last_line(self) -> None:
        last = format_review_comment(COMMENT).splitlines()[-1]

        assert last == "<https://github.com/Canon-Regularis/Shannon-bot/pull/7#discussion_r1>"

    def test_an_empty_body_leaves_the_quote_out(self) -> None:
        rendered = format_review_comment(replace(COMMENT, body=""))

        assert not any(line.startswith(">") for line in rendered.splitlines())
        assert "commented on" in rendered


class TestAPathNobodyChoseCarefully:
    def test_a_mention_in_a_file_name_does_not_ring_anybody(self) -> None:
        """A code span stops markdown reading a name. It does not stop Discord reading a mention,
        and `<@1234>` is a legal file name on every platform this runs against."""
        line = said(replace(COMMENT, path="<@1234>.py"))

        assert "<@1234>" not in line
        assert "1234" in line

    def test_a_backtick_cannot_break_out_of_the_span(self) -> None:
        line = said(replace(COMMENT, path="we`ird.py"))

        assert line.count("``") >= 1

    def test_a_path_too_long_to_print_keeps_its_file_name(self) -> None:
        """Cut at the front, because the end of a path is the part anybody reading it wants."""
        clipped = clipped_path("a/" * 200 + "module.py")

        assert len(clipped) == REVIEW_PATH_LIMIT
        assert clipped.endswith("module.py")
        assert clipped.startswith("…")

    def test_a_short_path_is_left_alone(self) -> None:
        assert clipped_path("shannon/services/notes.py") == "shannon/services/notes.py"

    def test_a_path_long_enough_to_be_the_whole_message_still_leaves_room_for_the_rest(
        self,
    ) -> None:
        """The reason the limit exists. The file and line are the FIRST line of the message and
        `fit` drops lines from the end, so an unbounded path would take the body and the link
        with it rather than being cut itself."""
        rendered = format_review_comment(replace(COMMENT, path="a/" * 2000 + "module.py"))

        assert len(rendered) <= MESSAGE_LIMIT
        assert "> This claim wants giving back on cancellation too." in rendered
        assert rendered.splitlines()[-1].endswith("#discussion_r1>")
