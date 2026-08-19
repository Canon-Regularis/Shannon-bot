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

    @pytest.mark.parametrize("action", ["edited", "deleted", ""])
    def test_only_creation_is_mirrored(self, action: str) -> None:
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
