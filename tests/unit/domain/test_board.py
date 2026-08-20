"""Reading a project board's column back as one of our statuses.

Deliberately more forgiving than the label matcher, and the tests say why: a label namespace is
shared with whatever else a repository labels things, so `done` there may mean anything. A Status
column is a small set somebody chose to describe this workflow, so a board that says `In Progress`
means the thing this bot calls IN_REVIEW.
"""

from __future__ import annotations

import pytest

from shannon.domain.board import status_from_column
from shannon.domain.enums import Status


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("Backlog", Status.BACKLOG),
        ("Not reviewed", Status.NOT_REVIEWED),
        ("In review", Status.IN_REVIEW),
        ("Ready for merge", Status.READY_FOR_MERGE),
        ("Done", Status.DONE),
    ],
)
def test_a_board_spelled_our_way_needs_no_translation(column: str, expected: Status) -> None:
    """The five names the requirements give read like board columns because that is what they
    are, so a board named after them costs no configuration at all."""
    assert status_from_column(column) is expected


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("Todo", Status.NOT_REVIEWED),
        ("To do", Status.NOT_REVIEWED),
        ("In Progress", Status.IN_REVIEW),
        ("Done", Status.DONE),
    ],
)
def test_githubs_own_default_board_is_understood(column: str, expected: Status) -> None:
    """`Todo`, `In Progress`, `Done` is what GitHub creates for you, so it is the board most
    people will point this at first."""
    assert status_from_column(column) is expected


@pytest.mark.parametrize("column", ["IN REVIEW", "in_review", "  In-Review  ", "in/review"])
def test_case_spacing_and_punctuation_do_not_matter(column: str) -> None:
    assert status_from_column(column) is Status.IN_REVIEW


def test_a_column_nobody_taught_us_is_not_guessed_at() -> None:
    """None rather than a default. Guessing NOT_REVIEWED would walk real work backwards on
    every poll, and a column named something else is a question for whoever named it."""
    assert status_from_column("Needs design input") is None


@pytest.mark.parametrize("column", [None, "", "   "])
def test_no_column_at_all_says_nothing(column: str | None) -> None:
    """A card can sit on a board with its Status field unset, which is not a status."""
    assert status_from_column(column) is None
