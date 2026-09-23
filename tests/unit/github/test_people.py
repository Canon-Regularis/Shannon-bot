"""Whether GitHub would take somebody being put on an item, decided without asking it.

Issue #106. Two of these refusals GitHub would make anyway, with a 422. They are made here for the
sentence rather than for the round trip: GitHub's own words for the author case name the endpoint
and the collaborator rule, which is true and is not what somebody in a Discord thread needed.
"""

from __future__ import annotations

import pytest

from shannon.domain.models import Actor, IssueSnapshot, PullRequestSnapshot, RepositorySnapshot
from shannon.github.people import assignee_change, assignment_refusal, reviewer_change

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1, owner="acme", name="widget", html_url="https://github.com/acme/widget"
)


def pull_request(*, author: Actor | None = None, reviewers: tuple[Actor, ...] = ()):
    return PullRequestSnapshot(
        repository=REPO,
        github_object_id=100,
        number=7,
        title="Add the endpoint",
        html_url="https://github.com/acme/widget/pull/7",
        state="open",
        author=author,
        reviewers=reviewers,
    )


def issue(*, assignees: tuple[Actor, ...] = ()):
    return IssueSnapshot(
        repository=REPO,
        github_object_id=200,
        number=12,
        title="Threads do not lock",
        html_url="https://github.com/acme/widget/issues/12",
        state="open",
        assignees=assignees,
    )


class TestAskingForAReview:
    def test_somebody_who_has_not_been_asked(self) -> None:
        change = reviewer_change("alice", pull_request(), adding=True)

        assert change.wanted is True
        assert change.refusal is None
        assert change.login == "alice"

    def test_somebody_already_asked(self) -> None:
        """A second ask is a 422, so without this the person gets an error they did not cause."""
        change = reviewer_change("alice", pull_request(reviewers=(Actor("alice"),)), adding=True)

        assert change.wanted is False
        assert change.already is True
        assert change.refusal is None, "a repeat was reported as something to put right"

    def test_the_person_who_opened_it(self) -> None:
        """GitHub refuses this as though they were not a collaborator, which for the author of
        the thing is a confusing way to be told no."""
        change = reviewer_change("alice", pull_request(author=Actor("alice")), adding=True)

        assert change.wanted is False
        assert "opened this pull request" in str(change.refusal)

    def test_an_item_nobody_is_recorded_as_having_opened(self) -> None:
        """A deleted account leaves the author empty, and that is not everybody."""
        change = reviewer_change("alice", pull_request(author=None), adding=True)

        assert change.wanted is True


class TestWithdrawingAReview:
    def test_somebody_who_was_asked(self) -> None:
        change = reviewer_change("alice", pull_request(reviewers=(Actor("alice"),)), adding=False)

        assert change.wanted is True

    def test_somebody_who_was_not(self) -> None:
        """The client swallows the 404 this would cause, so without the check it would report
        success for having done nothing."""
        change = reviewer_change("alice", pull_request(), adding=False)

        assert change.wanted is False
        assert change.already is True
        assert change.refusal is None

    def test_the_author_is_only_refused_when_being_added(self) -> None:
        """The author guard must not leak into the other direction: somebody can be the author
        and a reviewer at once if GitHub was asked before this bot existed."""
        snapshot = pull_request(author=Actor("alice"), reviewers=(Actor("alice"),))

        assert reviewer_change("alice", snapshot, adding=False).wanted is True


class TestPuttingSomebodyOnAnIssue:
    def test_somebody_not_on_it(self) -> None:
        change = assignee_change("alice", issue(), adding=True)

        assert change.wanted is True

    def test_somebody_already_on_it(self) -> None:
        change = assignee_change("alice", issue(assignees=(Actor("alice"),)), adding=True)

        assert change.wanted is False
        assert change.already is True
        assert change.refusal is None

    def test_taking_somebody_off(self) -> None:
        change = assignee_change("alice", issue(assignees=(Actor("alice"),)), adding=False)

        assert change.wanted is True

    def test_taking_off_somebody_who_was_never_on(self) -> None:
        change = assignee_change("alice", issue(), adding=False)

        assert change.wanted is False
        assert change.already is True
        assert change.refusal is None


class TestHowTwoLoginsAreCompared:
    @pytest.mark.parametrize(("stored", "asked"), [("Alice", "alice"), ("alice", "ALICE")])
    def test_case_does_not_make_two_people(self, stored: str, asked: str) -> None:
        """GitHub treats logins case insensitively and the stores lowercase for that reason, so a
        login off a snapshot and one out of `user_links` need not agree on case."""
        change = reviewer_change(asked, pull_request(reviewers=(Actor(stored),)), adding=True)

        assert change.wanted is False

    def test_the_author_is_compared_the_same_way(self) -> None:
        change = reviewer_change("ALICE", pull_request(author=Actor("alice")), adding=True)

        assert change.wanted is False

    def test_a_different_person_is_a_different_person(self) -> None:
        change = reviewer_change("bob", pull_request(reviewers=(Actor("alice"),)), adding=True)

        assert change.wanted is True


class TestWhyGitHubWouldNotTakeAnAssignee:
    """The sentence a refusal carries, which used to be the same guess whatever had happened.

    Issue #133. A collaborator with write access was told he had no access to the repository,
    because a login that had moved and a person who was never there produce the same 404 and the
    message named only one of them. These are the three answers apart.
    """

    def test_somebody_with_no_relationship_to_the_repository(self) -> None:
        said = assignment_refusal("newbie", "acme/widget", "none")

        assert said == (
            "GitHub does not have newbie as a collaborator on acme/widget, so it will not put "
            "them on anything there. Somebody who can administer the repository has to invite "
            "them first."
        )

    def test_somebody_who_can_only_read_it(self) -> None:
        """The case the old sentence was least true of: they are in the repository, and the thing
        they are missing is write access rather than access."""
        said = assignment_refusal("reader", "acme/widget", "read")

        assert said == (
            "reader can read acme/widget but cannot be assigned in it: GitHub takes an assignee "
            "with write access or better. Triage counts as read here, which is GitHub's own "
            "folding rather than a rule of this bot's."
        )

    def test_triage_arrives_as_read_and_the_sentence_says_so(self) -> None:
        """GitHub folds it before answering, so nothing here ever sees the word. Somebody looking
        at a triage member in the settings page and this line beside it deserves the join."""
        assert "Triage counts as read here" in assignment_refusal("t", "acme/widget", "read")

    @pytest.mark.parametrize("permission", ["write", "admin", "maintain"])
    def test_an_access_github_ought_to_have_accepted_is_named_as_it_came(
        self, permission: str
    ) -> None:
        """Verbatim, so a name GitHub stops folding one day still reads sensibly instead of
        falling into a bucket it does not belong in. `maintain` is the one to watch: it is folded
        onto write today, and nothing here would notice if it stopped being."""
        said = assignment_refusal("mona", "acme/widget", permission)

        assert said == (
            f"GitHub says mona has {permission} access to acme/widget and will still not take "
            "them as an assignee. Those two answers should agree, so the GitHub account linked "
            "to them is worth checking."
        )
