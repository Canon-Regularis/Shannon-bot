"""`pull_request_review_comment`, read off the body GitHub actually sends.

Issue #107. Everything saying where the comment points is optional, each in a different way, which
is what most of this is about: GitHub leaves `start_line` out of a single-line comment, leaves
`in_reply_to_id` out of one that opens a thread, and empties `line` on one the diff has moved out
from under. None of those is a broken payload and none of them may refuse the comment.
"""

from __future__ import annotations

import pytest

from shannon.domain.enums import ObjectType
from shannon.github.webhooks.review_comments import parse_review_comment_event
from tests.support import github_payloads as payloads


def test_an_inline_comment_parses() -> None:
    snapshot = parse_review_comment_event("created", payloads.pull_request_review_comment_event())

    assert snapshot is not None
    assert snapshot.item_number == 7
    assert snapshot.comment_id == payloads.REVIEW_COMMENT_ID
    assert snapshot.author is not None and snapshot.author.login == "monalisa"
    assert snapshot.body == "This claim wants giving back on cancellation too."
    assert snapshot.path == "shannon/services/notes.py"
    assert snapshot.line == 205
    assert snapshot.start_line is None
    assert snapshot.in_reply_to_id is None
    assert snapshot.created_at is not None
    assert f"discussion_r{payloads.REVIEW_COMMENT_ID}" in snapshot.html_url


def test_it_is_always_about_a_pull_request() -> None:
    """Fixed on the class rather than read off the body.

    The rebuild that mends a deleted thread branches on this, and its other arm reads the pull
    request as an issue, which GitHub serves and which opens a second thread for the same item.
    """
    snapshot = parse_review_comment_event("created", payloads.pull_request_review_comment_event())

    assert snapshot is not None
    assert snapshot.object_type is ObjectType.PR


def test_its_key_is_its_own() -> None:
    """GitHub numbers issue comments, reviews and review comments separately, so three notes can
    share a number and only the prefix tells them apart."""
    snapshot = parse_review_comment_event(
        "created", payloads.pull_request_review_comment_event(id=12)
    )

    assert snapshot is not None
    assert snapshot.note_key == "review-comment:12"


@pytest.mark.parametrize("action", ["edited", "deleted", "", "created "])
def test_only_a_new_comment_is_mirrored(action: str) -> None:
    assert parse_review_comment_event(action, payloads.pull_request_review_comment_event()) is None


class TestWhereTheCommentPoints:
    def test_a_multi_line_comment_carries_both_ends(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(start_line=200, line=205)
        )

        assert snapshot is not None
        assert (snapshot.start_line, snapshot.line) == (200, 205)

    def test_a_reply_says_which_comment_it_answers(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(in_reply_to_id=98765)
        )

        assert snapshot is not None
        assert snapshot.in_reply_to_id == 98765

    def test_a_comment_on_a_whole_file_has_no_line_at_all(self) -> None:
        """GitHub empties both the current line and the original one for a file-level comment,
        which is what tells it apart from one the diff has moved under."""
        snapshot = parse_review_comment_event(
            "created",
            payloads.pull_request_review_comment_event(
                subject_type="file", line=None, original_line=None
            ),
        )

        assert snapshot is not None
        assert (snapshot.line, snapshot.original_line) == (None, None)

    def test_an_outdated_comment_keeps_where_it_was_written(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(line=None, original_line=205)
        )

        assert snapshot is not None
        assert snapshot.line is None
        assert snapshot.original_line == 205

    def test_a_line_that_is_not_a_number_is_dropped_rather_than_carried(self) -> None:
        """Nothing GitHub sends looks like this. It is here because the body comes off the
        network, and a string where a number belongs must not reach the renderer."""
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(line="205")
        )

        assert snapshot is not None
        assert snapshot.line is None

    def test_a_missing_path_is_not_a_missing_comment(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["comment"]["path"]

        snapshot = parse_review_comment_event("created", payload)

        assert snapshot is not None
        assert snapshot.path == ""


class TestWhatItRefuses:
    def test_a_payload_without_a_comment(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["comment"]

        assert parse_review_comment_event("created", payload) is None

    def test_a_payload_without_a_repository(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["repository"]

        assert parse_review_comment_event("created", payload) is None

    def test_a_payload_without_a_pull_request(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["pull_request"]

        assert parse_review_comment_event("created", payload) is None

    def test_a_pull_request_without_a_number(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["pull_request"]["number"]

        assert parse_review_comment_event("created", payload) is None

    def test_a_comment_without_an_id(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["comment"]["id"]

        assert parse_review_comment_event("created", payload) is None


class TestWhatItSurvives:
    def test_a_deleted_account(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(user=None)
        )

        assert snapshot is not None
        assert snapshot.author is None

    def test_a_null_body(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(body=None)
        )

        assert snapshot is not None
        assert snapshot.body == ""

    def test_an_unparseable_timestamp(self) -> None:
        snapshot = parse_review_comment_event(
            "created", payloads.pull_request_review_comment_event(created_at="whenever")
        )

        assert snapshot is not None
        assert snapshot.created_at is None

    def test_a_missing_link(self) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["comment"]["html_url"]

        snapshot = parse_review_comment_event("created", payload)

        assert snapshot is not None
        assert snapshot.html_url == ""


class TestSayingWhyOneWasDropped:
    """A comment refused here reaches no thread, and nothing ever reads one back from GitHub, so
    the log line is the only record that it arrived at all."""

    def test_an_unusable_comment_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = payloads.pull_request_review_comment_event()
        payload["comment"]["id"] = None

        with caplog.at_level("WARNING"):
            assert parse_review_comment_event("created", payload) is None

        assert "usable comment" in caplog.text

    def test_a_missing_repository_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["repository"]

        with caplog.at_level("WARNING"):
            assert parse_review_comment_event("created", payload) is None

        assert "usable repository" in caplog.text

    def test_a_missing_number_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = payloads.pull_request_review_comment_event()
        del payload["pull_request"]["number"]

        with caplog.at_level("WARNING"):
            assert parse_review_comment_event("created", payload) is None

        assert "pull request number" in caplog.text

    def test_a_comment_that_parses_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            parsed = parse_review_comment_event(
                "created", payloads.pull_request_review_comment_event()
            )

        assert parsed is not None
        assert caplog.text == ""
