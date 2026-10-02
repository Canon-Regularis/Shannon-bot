"""Turning an issue_comment delivery into a snapshot.

GitHub sends this event for pull requests as well as issues, with the pull request dressed as
one, which is why nothing here filters on the kind.
"""

from __future__ import annotations

import pytest

from shannon.github.webhooks.comments import parse_comment_event
from tests.support import github_payloads as payloads


class TestCommentParsing:
    def test_a_comment_on_an_issue_parses(self) -> None:
        snapshot = parse_comment_event("created", payloads.issue_comment_event())

        assert snapshot is not None
        assert snapshot.item_number == 12
        assert snapshot.comment_id == payloads.COMMENT_ID
        assert snapshot.author is not None and snapshot.author.login == "monalisa"
        assert "Reproduced on main" in snapshot.body
        assert snapshot.created_at is not None

    def test_a_comment_on_a_pull_request_parses_the_same_way(self) -> None:
        payload = payloads.issue_comment_event(on=payloads.pull_request_as_issue())

        snapshot = parse_comment_event("created", payload)

        assert snapshot is not None
        assert snapshot.item_number == 7

    def test_an_edit_is_mirrored_too(self) -> None:
        """Issue #165. A comment edited on GitHub left the thread showing text that existed
        nowhere any more, because the parser turned the delivery away before anything saw it.

        The same payload shape as a creation, which is why the gate is the whole of the change
        here: nothing below this line reads the action again.
        """
        payload = payloads.issue_comment_event()
        created = parse_comment_event("created", payload)

        snapshot = parse_comment_event("edited", payload)

        assert snapshot is not None
        assert created is not None
        assert snapshot.note_key == created.note_key, "an edit must key on the comment it edits"

    @pytest.mark.parametrize("action", ["deleted", ""])
    def test_a_deletion_is_not(self, action: str) -> None:
        """Deletions stay out, unlike edits. The mirrored message is where a conversation
        happened and people reply under it, so removing it would take those replies out of
        their context - and nothing would ever put it back.
        """
        assert parse_comment_event(action, payloads.issue_comment_event()) is None

    def test_a_payload_without_a_comment_is_ignored(self) -> None:
        payload = payloads.issue_comment_event()
        del payload["comment"]

        assert parse_comment_event("created", payload) is None

    def test_a_comment_with_no_usable_id_is_ignored(self) -> None:
        """The id is the note key, so a comment without one cannot be claimed before posting.

        Mirroring it anyway would put it in the thread again on every retry.
        """
        payload = payloads.issue_comment_event()
        payload["comment"]["id"] = None

        assert parse_comment_event("created", payload) is None

    def test_a_payload_without_an_item_number_is_ignored(self) -> None:
        payload = payloads.issue_comment_event()
        del payload["issue"]["number"]

        assert parse_comment_event("created", payload) is None

    def test_a_payload_without_a_repository_is_ignored(self) -> None:
        payload = payloads.issue_comment_event()
        del payload["repository"]

        assert parse_comment_event("created", payload) is None

    def test_an_empty_body_still_parses(self) -> None:
        snapshot = parse_comment_event("created", payloads.issue_comment_event(body=""))

        assert snapshot is not None
        assert snapshot.body == ""


class TestSayingWhyOneWasDropped:
    """A note that is refused here reaches nobody and leaves no row, so the log line is the only
    thing standing between "the comment never appeared" and knowing why.

    Written because inverting the condition that decides whether to log went unnoticed by the
    whole unit tier: every refusal was silent and every success carried the warning, and nothing
    said so.
    """

    def test_an_unusable_comment_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = payloads.issue_comment_event()
        payload["comment"]["id"] = None

        with caplog.at_level("WARNING"):
            assert parse_comment_event("created", payload) is None

        assert "usable comment" in caplog.text

    def test_a_comment_that_parses_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """A warning on the ordinary path is worse than none: it teaches the reader to skip it."""
        with caplog.at_level("WARNING"):
            assert parse_comment_event("created", payloads.issue_comment_event()) is not None

        assert caplog.text == ""
