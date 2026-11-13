"""Who a message is for, decided before anybody is looked up in a table.

Issue #164. A CI result used to ring the requested reviewers when the jobs passed, so the people
who caused a run were told nothing and the people who had not looked at it yet were interrupted.
The audience is the whole of that defect, and it is a pure function, so it is tested here rather
than through a webhook.

What the two helpers below have to get right is one thing: a set of people assembled from lists
GitHub keeps apart, where somebody can appear in several of them and the same account can be
spelled with different capitals in each. A name said twice is a person rung twice for one event.
"""

from __future__ import annotations

from shannon.domain.models import Actor, CommitRef, PullRequestSnapshot, RepositorySnapshot
from shannon.services.audience import (
    PEOPLE_RUNG,
    author_and_assignees,
    author_and_committers,
    everyone_who_worked_on_it,
)

REPO = RepositorySnapshot(
    github_repo_id=1, owner="Canon-Regularis", name="Shannon-bot", html_url="https://github.com/o/n"
)


def a_pull_request(
    author: str | None = "octocat", assignees: tuple[str, ...] = ()
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repository=REPO,
        github_object_id=1,
        number=7,
        title="Add the webhook endpoint",
        html_url="https://github.com/o/n/pull/7",
        state="open",
        author=Actor(author) if author is not None else None,
        assignees=tuple(Actor(login) for login in assignees),
    )


def a_commit(login: str | None = "hubot", *, merge: bool = False, sha: str = "a" * 40) -> CommitRef:
    return CommitRef(
        sha=sha,
        message="Add the thing",
        author=Actor(login) if login is not None else None,
        merge=merge,
    )


def logins(people: tuple[Actor, ...]) -> list[str]:
    return [person.login for person in people]


class TestWhoAPullRequestWaitsOn:
    def test_the_author_leads_and_the_assignees_follow(self) -> None:
        item = a_pull_request(author="octocat", assignees=("hubot", "monalisa"))

        assert logins(author_and_assignees(item)) == ["octocat", "hubot", "monalisa"]

    def test_an_author_who_is_also_assigned_is_named_once(self) -> None:
        """Asserted on the count and the lowered name, not on the capitals. The dedupe is a dict
        keyed on the lowered login, so the FIRST appearance fixes the position and the LAST fixes
        which spelling survives - and neither matters, because `resolve_many` lowers the name
        again before it looks anybody up. What would matter is being named twice."""
        item = a_pull_request(author="octocat", assignees=("OctoCat", "hubot"))

        people = author_and_assignees(item)

        assert [login.lower() for login in logins(people)] == ["octocat", "hubot"]

    def test_a_pull_request_with_no_author_is_still_an_answer(self) -> None:
        """GitHub sends a null user for a deleted account, and the assignees are still somebody."""
        item = a_pull_request(author=None, assignees=("hubot",))

        assert logins(author_and_assignees(item)) == ["hubot"]


class TestEverybodyACheckResultIsNewsTo:
    def test_whoever_wrote_the_code_is_added_to_the_author_and_assignees(self) -> None:
        item = a_pull_request(author="octocat", assignees=("hubot",))

        people = everyone_who_worked_on_it(item, [a_commit("defunkt")])

        assert logins(people) == ["octocat", "hubot", "defunkt"]

    def test_a_contributor_who_is_also_the_author_is_named_once(self) -> None:
        """Cased differently on purpose. GitHub echoes a login the way it was typed in each place
        it appears, so the dedupe has to be on the lowered name or the author is rung twice for
        their own push."""
        item = a_pull_request(author="octocat")

        people = everyone_who_worked_on_it(item, [a_commit("OctoCat")])

        assert len(people) == 1, "the author was rung twice for their own push"
        assert people[0].login.lower() == "octocat"

    def test_the_author_keeps_their_place_when_they_also_wrote_a_commit(self) -> None:
        item = a_pull_request(author="octocat", assignees=("hubot",))

        people = everyone_who_worked_on_it(item, [a_commit("hubot"), a_commit("defunkt")])

        assert logins(people) == ["octocat", "hubot", "defunkt"]

    def test_a_commit_github_could_not_link_to_an_account_adds_nobody(self) -> None:
        """Not a gap. The address it was written under holds no GitHub account, so there is
        nobody to ring, and saying nothing is the honest answer rather than a missing name."""
        item = a_pull_request(author="octocat")

        people = everyone_who_worked_on_it(item, [a_commit(None), a_commit("defunkt")])

        assert logins(people) == ["octocat", "defunkt"]

    def test_a_merge_commit_does_not_put_whoever_pressed_update_branch_in_the_audience(
        self,
    ) -> None:
        """The side door issue #164 would otherwise come back through.

        A merge commit's account is whoever pressed "Update branch", which is very often a
        reviewer tidying somebody else's pull request. The commit never leaves the branch, so
        keeping it would ring that reviewer on every suite for the rest of the pull request's
        life - the same wrong person, by a different route.
        """
        item = a_pull_request(author="octocat")

        people = everyone_who_worked_on_it(item, [a_commit("monalisa", merge=True)])

        assert logins(people) == ["octocat"]

    def test_a_merge_does_not_hide_a_real_commit_by_the_same_person(self) -> None:
        """The other half of the filter: dropping merges must drop the COMMIT, not the person.
        Somebody who merged the base in and also wrote code is still a contributor."""
        item = a_pull_request(author="octocat")

        people = everyone_who_worked_on_it(
            item, [a_commit("monalisa", merge=True), a_commit("monalisa", sha="c" * 40)]
        )

        assert logins(people) == ["octocat", "monalisa"]

    def test_a_pull_request_whose_commits_are_unknown_is_the_author_and_assignees(self) -> None:
        """What the caller passes when GitHub would not list them. The audience narrows rather
        than the message being lost."""
        item = a_pull_request(author="octocat", assignees=("hubot",))

        assert logins(everyone_who_worked_on_it(item, [])) == ["octocat", "hubot"]

    def test_the_audience_is_capped_with_the_author_kept(self) -> None:
        """Discord refuses a message naming more than a hundred accounts, and `may_be_pinged` has
        no cap of its own because every caller before this one was bounded by GitHub's limits on
        assignees and reviewers. A long-lived branch is bounded by nothing, so the cap is here,
        and it sheds contributors rather than the people the result is actually about.
        """
        item = a_pull_request(author="octocat", assignees=("hubot",))
        crowd = [a_commit(f"person{index}", sha=f"{index:040d}") for index in range(PEOPLE_RUNG)]

        people = everyone_who_worked_on_it(item, crowd)

        assert len(people) == PEOPLE_RUNG
        assert logins(people)[:2] == ["octocat", "hubot"]


class TestWhoAReviewAskingForChangesIsFor:
    """Issue #263. With no files to name, a review asking for changes is for the pull request's
    author and whoever wrote its commits - and nobody else, which is where it parts from a CI
    result."""

    def test_the_author_leads_and_the_committers_follow_in_order(self) -> None:
        people = author_and_committers(
            Actor("octocat"), [a_commit("defunkt"), a_commit("hubot", sha="b" * 40)]
        )

        assert logins(people) == ["octocat", "defunkt", "hubot"]

    def test_an_author_who_also_committed_is_named_once_whatever_the_case(self) -> None:
        people = author_and_committers(Actor("octocat"), [a_commit("OctoCat")])

        assert len(people) == 1, "the author was rung twice for their own commit"
        assert people[0].login.lower() == "octocat"

    def test_somebody_with_two_commits_is_named_once(self) -> None:
        people = author_and_committers(
            Actor("octocat"), [a_commit("defunkt"), a_commit("DEFUNKT", sha="b" * 40)]
        )

        assert [login.lower() for login in logins(people)] == ["octocat", "defunkt"]

    def test_a_merge_and_a_commit_with_no_account_add_nobody(self) -> None:
        """The same rule as a CI result, by the same code: whoever pressed "Update branch" made
        none of the code a review is asking to change."""
        people = author_and_committers(
            Actor("octocat"), [a_commit("monalisa", merge=True), a_commit(None, sha="b" * 40)]
        )

        assert logins(people) == ["octocat"]

    def test_a_deleted_author_leaves_the_committers(self) -> None:
        """GitHub sends a null user for a deleted account, and whoever wrote the code is still
        somebody."""
        assert logins(author_and_committers(None, [a_commit("defunkt")])) == ["defunkt"]

    def test_nobody_at_all_is_an_empty_answer(self) -> None:
        assert author_and_committers(None, []) == ()

    def test_the_assignees_are_not_asked(self) -> None:
        """Where it parts from `everyone_who_worked_on_it`: it takes the author on their own, so
        an assignee who wrote none of the code cannot be in the answer."""
        item = a_pull_request(author="octocat", assignees=("hubot",))

        people = author_and_committers(item.author, [a_commit("defunkt")])

        assert logins(people) == ["octocat", "defunkt"]

    def test_the_answer_is_capped_with_the_author_kept(self) -> None:
        crowd = [a_commit(f"person{index}", sha=f"{index:040d}") for index in range(PEOPLE_RUNG)]

        people = author_and_committers(Actor("octocat"), crowd)

        assert len(people) == PEOPLE_RUNG
        assert logins(people)[0] == "octocat"
