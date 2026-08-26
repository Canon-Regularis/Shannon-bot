from __future__ import annotations

import pytest

from shannon.github.webhooks.reviews import parse_review_event
from tests.support import github_payloads as payloads


def test_a_submitted_review_parses() -> None:
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event())

    assert snapshot is not None
    assert snapshot.item_number == 7
    assert snapshot.review_id == payloads.REVIEW_ID
    assert snapshot.author is not None and snapshot.author.login == "monalisa"
    assert snapshot.body == "Looks right, one nit inline."
    assert snapshot.verdict == "approved"
    assert snapshot.created_at is not None
    assert f"pullrequestreview-{payloads.REVIEW_ID}" in snapshot.html_url


@pytest.mark.parametrize("state", ["approved", "changes_requested", "commented"])
def test_every_verdict_a_submission_can_carry(state: str) -> None:
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event(state=state))

    assert snapshot is not None
    assert snapshot.verdict == state


def test_an_uppercase_state_is_normalised() -> None:
    """The REST API reports APPROVED where webhooks report approved."""
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event(state="APPROVED"))

    assert snapshot is not None
    assert snapshot.verdict == "approved"


@pytest.mark.parametrize("action", ["edited", "dismissed", "", "submitted "])
def test_only_submission_is_mirrored(action: str) -> None:
    assert parse_review_event(action, payloads.pull_request_review_event()) is None


def test_an_empty_body_still_parses() -> None:
    """Approving without writing anything is the common case."""
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event(body=""))

    assert snapshot is not None
    assert snapshot.body == ""
    assert snapshot.verdict == "approved"


def test_a_null_body_does_not_crash() -> None:
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event(body=None))

    assert snapshot is not None
    assert snapshot.body == ""


def test_a_payload_without_a_review_is_ignored() -> None:
    payload = payloads.pull_request_review_event()
    del payload["review"]

    assert parse_review_event("submitted", payload) is None


def test_a_payload_without_a_repository_is_ignored() -> None:
    payload = payloads.pull_request_review_event()
    del payload["repository"]

    assert parse_review_event("submitted", payload) is None


def test_a_payload_without_a_pull_request_number_is_ignored() -> None:
    payload = payloads.pull_request_review_event()
    del payload["pull_request"]["number"]

    assert parse_review_event("submitted", payload) is None


def test_a_review_without_an_id_is_ignored() -> None:
    payload = payloads.pull_request_review_event()
    del payload["review"]["id"]

    assert parse_review_event("submitted", payload) is None


def test_a_deleted_reviewer_account_does_not_crash() -> None:
    snapshot = parse_review_event("submitted", payloads.pull_request_review_event(user=None))

    assert snapshot is not None
    assert snapshot.author is None


def test_an_unparseable_timestamp_becomes_none() -> None:
    snapshot = parse_review_event(
        "submitted", payloads.pull_request_review_event(submitted_at="whenever")
    )

    assert snapshot is not None
    assert snapshot.created_at is None


class TestSayingWhyOneWasDropped:
    """The same gap the comment parser had, in the same shape and the same line.

    A review refused here closes no request and reaches no thread, and the log line is the only
    record that it arrived at all.
    """

    def test_an_unusable_review_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = payloads.pull_request_review_event()
        payload["review"]["id"] = None

        with caplog.at_level("WARNING"):
            assert parse_review_event("submitted", payload) is None

        assert "usable review" in caplog.text

    def test_a_review_that_parses_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            assert parse_review_event("submitted", payloads.pull_request_review_event()) is not None

        assert caplog.text == ""
