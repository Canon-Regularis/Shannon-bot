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


def test_an_edit_is_mirrored_too() -> None:
    """Issue #165. A review body is a comment with a verdict on it and goes through the same
    mirror, so an edited one was stale in exactly the same way."""
    payload = payloads.pull_request_review_event()
    submitted = parse_review_event("submitted", payload)

    snapshot = parse_review_event("edited", payload)

    assert snapshot is not None
    assert submitted is not None
    assert snapshot.note_key == submitted.note_key, "an edit must key on the review it edits"


@pytest.mark.parametrize("action", ["dismissed", "", "submitted "])
def test_a_dismissal_or_a_malformed_action_is_not(action: str) -> None:
    """`dismissed` stays out although `edited` came in: it changes where the review stands on
    GitHub, not a word of what the review says, and the mirror shows what it says."""
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


class TestWhatAReviewAskingForChangesNeedsToKnow:
    """Issue #263. A review asking for changes rings the people who made the code it is about, and
    finding them starts from two things the webhook already carries: the commit the review was made
    on, and who opened the pull request."""

    def test_a_review_knows_the_commit_it_was_made_on(self) -> None:
        snapshot = parse_review_event(
            "submitted", payloads.pull_request_review_event(commit_id="d" * 40)
        )

        assert snapshot is not None
        assert snapshot.commit_id == "d" * 40

    def test_a_review_with_no_commit_has_an_empty_one(self) -> None:
        """Empty rather than missing, which reads downstream as a commit nobody can find the
        history of a file at, and so as the one line that names everybody."""
        payload = payloads.pull_request_review_event()
        del payload["review"]["commit_id"]

        snapshot = parse_review_event("submitted", payload)

        assert snapshot is not None
        assert snapshot.commit_id == ""

    def test_a_commit_that_is_not_text_is_no_commit(self) -> None:
        snapshot = parse_review_event("submitted", payloads.pull_request_review_event(commit_id=7))

        assert snapshot is not None
        assert snapshot.commit_id == ""

    def test_a_review_knows_who_opened_the_pull_request(self) -> None:
        """Off the webhook's own `pull_request` block, so nothing asks GitHub for it."""
        snapshot = parse_review_event("submitted", payloads.pull_request_review_event())

        assert snapshot is not None
        assert snapshot.pull_request_author is not None
        assert snapshot.pull_request_author.login == "octocat"
        assert snapshot.pull_request_author.github_user_id == 583231

    def test_whoever_opened_it_is_not_whoever_reviewed_it(self) -> None:
        snapshot = parse_review_event("submitted", payloads.pull_request_review_event())

        assert snapshot is not None
        assert snapshot.author is not None and snapshot.author.login == "monalisa"
        assert snapshot.pull_request_author is not None
        assert snapshot.pull_request_author.login != snapshot.author.login

    def test_a_pull_request_opened_by_a_deleted_account_has_no_opener(self) -> None:
        payload = payloads.pull_request_review_event()
        payload["pull_request"]["user"] = None

        snapshot = parse_review_event("submitted", payload)

        assert snapshot is not None
        assert snapshot.pull_request_author is None

    def test_a_freshly_parsed_review_asks_nobody_yet(self) -> None:
        """Whom it asks takes GitHub reads the mirror makes, so off the webhook it is nobody."""
        snapshot = parse_review_event(
            "submitted", payloads.pull_request_review_event(state="changes_requested")
        )

        assert snapshot is not None
        assert snapshot.changes_asked is None
        assert snapshot.replying_to == ()
