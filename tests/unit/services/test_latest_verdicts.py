"""What a pull request's reviews currently say, reduced from every one ever submitted.

Issue #155. GitHub hands back the whole history rather than the current state, so the reduction
is where "everyone approved" is actually decided, and getting it wrong is the difference between
announcing agreement and announcing it when somebody has asked for changes.

Two rules carry the file. A comment left after an approval must not clear it, because a comment
is a note rather than a verdict and GitHub's own rule ignores it too. And a dismissal MUST clear
one, because a dismissed review is the same row rewritten — which is the whole reason this is
read from GitHub rather than tallied from the webhooks that arrive, since `dismissed` is not an
action this bot subscribes to.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shannon.domain.models import Actor, RepositorySnapshot, ReviewSnapshot
from shannon.services.reviews import latest_verdicts

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)


def a_review(review_id: int, state: str, login: str | None = "monalisa") -> ReviewSnapshot:
    return ReviewSnapshot(
        repository=REPO,
        item_number=7,
        review_id=review_id,
        state=state,
        author=Actor(login, 200) if login is not None else None,
        body="",
        html_url="",
        created_at=datetime(2026, 8, 11, 11, 0, tzinfo=UTC),
    )


class TestWhatCounts:
    def test_nothing_reduces_to_nothing(self) -> None:
        """Which is not the same as everybody approving, and the caller has to tell them apart:
        every verdict in an empty mapping is an approval."""
        assert latest_verdicts([]) == {}

    def test_one_approval_is_one_verdict(self) -> None:
        assert latest_verdicts([a_review(1, "approved")]) == {"monalisa": "approved"}

    def test_the_rest_api_spelling_is_flattened(self) -> None:
        """REST sends the state uppercased and webhooks send it lowercased, which is what
        `ReviewSnapshot.verdict` exists to settle. This reads one, so it reads the REST one."""
        assert latest_verdicts([a_review(1, "APPROVED")]) == {"monalisa": "approved"}

    def test_two_people_are_two_entries(self) -> None:
        found = latest_verdicts(
            [a_review(1, "approved", "octocat"), a_review(2, "changes_requested", "monalisa")]
        )

        assert found == {"octocat": "approved", "monalisa": "changes_requested"}

    def test_one_person_under_two_spellings_is_one_entry(self) -> None:
        """GitHub answers in whatever case an account uses, and the caller's other lists are
        lowered too, so anything else would count somebody twice."""
        found = latest_verdicts(
            [a_review(1, "approved", "Alice"), a_review(2, "approved", "alice")]
        )

        assert found == {"alice": "approved"}


class TestWhatReplacesWhat:
    def test_the_later_verdict_wins(self) -> None:
        found = latest_verdicts([a_review(1, "changes_requested"), a_review(2, "approved")])

        assert found == {"monalisa": "approved"}

    def test_a_comment_does_not_clear_an_approval(self) -> None:
        """The rule this reduction exists for. Somebody approves, then answers a question in the
        thread; GitHub wraps that answer in a `commented` review, and counting it would leave a
        pull request looking unreviewed by the person who had just approved it."""
        found = latest_verdicts([a_review(1, "approved"), a_review(2, "commented")])

        assert found == {"monalisa": "approved"}

    def test_a_started_review_nobody_submitted_does_not_count(self) -> None:
        """GitHub shows a viewer only their own pending reviews, so an App token ordinarily sees
        none — but one that was visible would otherwise read as a verdict nobody gave."""
        assert latest_verdicts([a_review(1, "pending")]) == {}

    def test_a_dismissal_does_clear_an_approval(self) -> None:
        """The other half, and the reason this is read from GitHub at all. Dismissing rewrites
        the review in place, and `dismissed` is not an action this bot subscribes to — so a tally
        kept from the webhooks that arrive would go on counting an approval somebody took back.
        """
        found = latest_verdicts([a_review(1, "dismissed"), a_review(2, "commented")])

        assert found == {"monalisa": "dismissed"}

    def test_the_order_is_the_id_and_not_the_order_they_arrived(self) -> None:
        """GitHub documents chronological order but does not promise it, and a review's id is
        assigned when it is submitted. Handed the same two rows backwards, the answer must not
        change."""
        forwards = [a_review(1, "changes_requested"), a_review(2, "approved")]

        assert latest_verdicts(list(reversed(forwards))) == latest_verdicts(forwards)


class TestAnAccountThatHasGone:
    def test_it_is_skipped_rather_than_counted(self) -> None:
        """GitHub answers with a null user for a deleted account. It cannot be counted either
        way: there is nobody to attribute the verdict to, and nobody who could change it."""
        found = latest_verdicts([a_review(1, "approved", None), a_review(2, "approved")])

        assert found == {"monalisa": "approved"}

    def test_a_pull_request_reviewed_only_by_one_reduces_to_nothing(self) -> None:
        assert latest_verdicts([a_review(1, "approved", None)]) == {}
