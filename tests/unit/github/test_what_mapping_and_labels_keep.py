"""What the payload readers and the label arithmetic answer, pinned across #271's merge of copies.

Characterisation. #271 folds hand-written copies into one helper each: "a string, else empty" in
`mapping`, "parse every row, drop what will not parse" in the list readers, and the two
single-valued label changes in `labels`. Each copy answers today the way the others do, and these
pin the answers, so the merge has to keep them rather than settle on whichever copy it kept.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from shannon.domain.enums import Priority
from shannon.domain.models import Actor, CheckRun, CommitRef, Label, RepositorySnapshot
from shannon.github import mapping
from shannon.github.labels import PRIORITY_LABELS, LabelChange, priority_change, status_change
from shannon.github.labels import status_label as status_definition

REPO = RepositorySnapshot(
    github_repo_id=1, owner="acme", name="widget", html_url="https://github.com/acme/widget"
)

# Present, and not a string. Every one of these reads as "" on the fields below.
NOT_TEXT: list[object] = [None, 0, 7, 12.5, True, [], ["x"], {}, {"x": "y"}]

ISSUE = {"id": 10, "number": 4}
REVIEW = {"id": 20}
INLINE = {"id": 30}
COMMIT = {"sha": "a" * 40}
RUN = {"id": 40, "name": "tests"}


def _title(value: object) -> str:
    found = mapping.issue({**ISSUE, "title": value}, REPO)
    assert found is not None
    return found.title


def _issue_body(value: object) -> str:
    found = mapping.issue({**ISSUE, "body": value}, REPO)
    assert found is not None
    return found.body


def _head_sha(value: object) -> str:
    found = mapping.pull_request({**ISSUE, "head": {"sha": value}}, REPO)
    assert found is not None
    return found.head_sha


def _review_state(value: object) -> str:
    found = mapping.review({**REVIEW, "state": value}, REPO, item_number=4)
    assert found is not None
    return found.state


def _review_commit(value: object) -> str:
    found = mapping.review({**REVIEW, "commit_id": value}, REPO, item_number=4)
    assert found is not None
    return found.commit_id


def _review_body(value: object) -> str:
    found = mapping.review({**REVIEW, "body": value}, REPO, item_number=4)
    assert found is not None
    return found.body


def _review_url(value: object) -> str:
    found = mapping.review({**REVIEW, "html_url": value}, REPO, item_number=4)
    assert found is not None
    return found.html_url


def _inline_path(value: object) -> str:
    found = mapping.review_comment({**INLINE, "path": value}, REPO, item_number=4)
    assert found is not None
    return found.path


def _comment_body(value: object) -> str:
    found = mapping.comment({**INLINE, "body": value}, REPO, item_number=4, on={})
    assert found is not None
    return found.body


def _comment_url(value: object) -> str:
    found = mapping.comment({**INLINE, "html_url": value}, REPO, item_number=4, on={})
    assert found is not None
    return found.html_url


def _commit_message(value: object) -> str:
    found = mapping.commit_ref({**COMMIT, "commit": {"message": value}})
    assert found is not None
    return found.message


def _run_status(value: object) -> str:
    found = mapping.check_run({**RUN, "status": value})
    assert found is not None
    return found.status


def _run_conclusion(value: object) -> str:
    found = mapping.check_run({**RUN, "conclusion": value})
    assert found is not None
    return found.conclusion


def _run_url(value: object) -> str:
    found = mapping.check_run({**RUN, "html_url": value})
    assert found is not None
    return found.html_url


TEXT_OR_EMPTY: dict[str, Callable[[object], str]] = {
    "an item's title": _title,
    "an item's body": _issue_body,
    "a pull request's head sha": _head_sha,
    "a review's state": _review_state,
    "a review's commit": _review_commit,
    "a review's body": _review_body,
    "a review's link": _review_url,
    "an inline comment's file": _inline_path,
    "a comment's body": _comment_body,
    "a comment's link": _comment_url,
    "a commit's message": _commit_message,
    "a check run's status": _run_status,
    "a check run's conclusion": _run_conclusion,
    "a check run's link": _run_url,
}

each_text_field = pytest.mark.parametrize(
    "read", list(TEXT_OR_EMPTY.values()), ids=list(TEXT_OR_EMPTY)
)


class TestAFieldThatIsTextOrNothing:
    @each_text_field
    @pytest.mark.parametrize("value", NOT_TEXT)
    def test_anything_but_a_string_reads_as_empty(
        self, read: Callable[[object], str], value: object
    ) -> None:
        assert read(value) == ""

    @each_text_field
    @pytest.mark.parametrize("value", ["", "  ", "written"])
    def test_a_string_is_kept_exactly_as_sent(
        self, read: Callable[[object], str], value: str
    ) -> None:
        """Even empty or blank: these fields coerce the type and judge nothing else."""
        assert read(value) == value

    @pytest.mark.parametrize("value", NOT_TEXT)
    def test_a_state_that_is_not_a_string_reads_as_open_rather_than_empty(
        self, value: object
    ) -> None:
        """The one field beside them with a different fallback."""
        found = mapping.issue({**ISSUE, "state": value}, REPO)

        assert found is not None and found.state == "open"

    @pytest.mark.parametrize("value", NOT_TEXT)
    def test_a_label_colour_that_is_not_a_string_is_none_rather_than_empty(
        self, value: object
    ) -> None:
        assert mapping.labels([{"name": "bug", "color": value}]) == (Label("bug", None),)

    def test_a_missing_field_reads_the_same_as_a_null_one(self) -> None:
        found = mapping.issue(ISSUE, REPO)

        assert found is not None
        assert (found.title, found.body, found.state) == ("", "", "open")


OCTOCAT = {"login": "octocat", "id": 1}
HUBOT = {"login": "hubot", "id": 2}


class TestAListWithRowsThatWillNotParse:
    """Every row that parses, in the order sent, and the rest dropped without a word."""

    def test_actors(self) -> None:
        rows = [OCTOCAT, None, {"login": ""}, "hubot", {"id": 3}, [OCTOCAT], HUBOT]

        assert mapping.actors(rows) == (Actor("octocat", 1), Actor("hubot", 2))

    def test_teams(self) -> None:
        rows = [{"slug": "core"}, None, {"slug": ""}, "core", {"name": "Docs Team"}, {"id": 5}]

        assert mapping.teams(rows) == (Actor("core"), Actor("Docs Team"))

    def test_labels(self) -> None:
        rows = [{"name": "bug", "color": "d73a4a"}, None, {"name": ""}, "docs", {"name": "docs"}]

        assert mapping.labels(rows) == (Label("bug", "d73a4a"), Label("docs", None))

    def test_check_runs(self) -> None:
        rows = [RUN, None, {"name": "lint"}, {"id": 41}, "tests", {"id": 42, "name": "lint"}]

        found = mapping.check_runs({"check_runs": rows})

        assert found == [
            CheckRun(check_run_id=40, name="tests", status="", conclusion="", html_url=""),
            CheckRun(check_run_id=42, name="lint", status="", conclusion="", html_url=""),
        ]

    def test_the_commits_in_a_compare(self) -> None:
        rows = [COMMIT, None, {"sha": ""}, "b" * 40, {"sha": "c" * 40}]

        found = mapping.commit_range({"status": "ahead", "commits": rows})

        assert found is not None
        assert found.commits == (
            CommitRef(sha="a" * 40, message="", author=None, merge=False),
            CommitRef(sha="c" * 40, message="", author=None, merge=False),
        )
        assert found.total == 2, "counted from what parsed when GitHub sends no total"

    @pytest.mark.parametrize("rows", [None, "x", {}, 3])
    def test_a_list_that_is_not_a_list_is_empty(self, rows: object) -> None:
        assert mapping.actors(rows) == ()
        assert mapping.teams(rows) == ()
        assert mapping.labels(rows) == ()
        assert mapping.check_runs({"check_runs": rows}) == []

    def test_the_readers_answer_tuples_except_check_runs_which_is_a_list(self) -> None:
        """`test_mapping` compares check runs with `[]`; the others are frozen into snapshots."""
        assert type(mapping.actors([OCTOCAT])) is tuple
        assert type(mapping.teams([{"slug": "core"}])) is tuple
        assert type(mapping.labels([{"name": "bug"}])) is tuple
        assert type(mapping.check_runs({"check_runs": [RUN]})) is list


IN_REVIEW = status_definition("In review")
HIGH = PRIORITY_LABELS[Priority.HIGH]


class TestAStatusChange:
    def test_an_item_already_carrying_it_changes_nothing(self) -> None:
        assert status_change(["status: In review", "bug"], IN_REVIEW) == LabelChange((), "")

    def test_a_stale_one_comes_off_as_the_wanted_one_goes_on(self) -> None:
        change = status_change(["status: Backlog", "bug"], IN_REVIEW)

        assert change == LabelChange(("status: Backlog",), "status: In review", IN_REVIEW)

    def test_every_stale_one_comes_off_in_the_order_held(self) -> None:
        held = ["BACKLOG", "bug", "status: Done", "status: Ready To Merge", "NOT_REVIEWED"]

        change = status_change(held, IN_REVIEW)

        assert change == LabelChange(
            ("BACKLOG", "status: Done", "status: Ready To Merge", "NOT_REVIEWED"),
            "status: In review",
            IN_REVIEW,
        )

    def test_a_stale_one_beside_the_wanted_one_comes_off_and_nothing_goes_on(self) -> None:
        change = status_change(["status: Done", "status: In review"], IN_REVIEW)

        assert change == LabelChange(("status: Done",), "")

    @pytest.mark.parametrize(
        "held", ["STATUS: IN REVIEW", "status: in review", "  status: In review "]
    )
    def test_the_wanted_one_in_another_case_or_padded_counts_as_carried(self, held: str) -> None:
        assert status_change([held], IN_REVIEW) == LabelChange((), "")

    def test_the_old_spelling_of_the_same_status_is_stale(self) -> None:
        """`IN_REVIEW` reads as the same status, and is still not the label being written."""
        change = status_change(["in_review"], IN_REVIEW)

        assert change == LabelChange(("in_review",), "status: In review", IN_REVIEW)

    def test_an_ordinary_label_is_never_taken_off(self) -> None:
        change = status_change(["shipped", "high"], IN_REVIEW)

        assert change == LabelChange((), "status: In review", IN_REVIEW)


class TestAPriorityChange:
    def test_an_item_already_carrying_it_changes_nothing(self) -> None:
        assert priority_change(["priority: High", "bug"], Priority.HIGH) == LabelChange((), "")

    def test_a_stale_one_comes_off_as_the_wanted_one_goes_on(self) -> None:
        change = priority_change(["priority: Low", "bug"], Priority.HIGH)

        assert change == LabelChange(("priority: Low",), "priority: High", HIGH)

    def test_every_spelling_the_parser_reads_comes_off_in_the_order_held(self) -> None:
        held = ["urgent", "bug", "LOW", "priority: Medium", "HIGH"]

        change = priority_change(held, Priority.HIGH)

        assert change == LabelChange(
            ("urgent", "LOW", "priority: Medium", "HIGH"), "priority: High", HIGH
        )

    def test_a_stale_one_beside_the_wanted_one_comes_off_and_nothing_goes_on(self) -> None:
        change = priority_change(["minor", "priority: High"], Priority.HIGH)

        assert change == LabelChange(("minor",), "")

    @pytest.mark.parametrize("held", ["PRIORITY: HIGH", "priority: high", " priority: High  "])
    def test_the_wanted_one_in_another_case_or_padded_counts_as_carried(self, held: str) -> None:
        assert priority_change([held], Priority.HIGH) == LabelChange((), "")

    def test_a_status_label_named_like_a_priority_is_never_taken_off(self) -> None:
        change = priority_change(["status: High priority"], Priority.LOW)

        assert change == LabelChange((), "priority: Low", PRIORITY_LABELS[Priority.LOW])
