from __future__ import annotations

import pytest

from shannon.domain.enums import Priority
from shannon.domain.priority import parse_priority


@pytest.mark.parametrize("label", ["HIGH", "high", "High", "  high  "])
def test_a_bare_word_is_a_priority(label: str) -> None:
    assert parse_priority([label]) is Priority.HIGH


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("priority: high", Priority.HIGH),
        ("priority: medium", Priority.MEDIUM),
        ("priority: low", Priority.LOW),
        ("priority-high", Priority.HIGH),
        ("priority/low", Priority.LOW),
        ("Priority: Medium", Priority.MEDIUM),
    ],
)
def test_prefixed_labels(label: str, expected: Priority) -> None:
    assert parse_priority([label]) is expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("HIGH_PRIORITY", Priority.HIGH),
        ("MED_PRIORITY", Priority.MEDIUM),
        ("LOW_PRIORITY", Priority.LOW),
        ("high priority", Priority.HIGH),
        ("Low Priority", Priority.LOW),
    ],
)
def test_suffixed_labels(label: str, expected: Priority) -> None:
    assert parse_priority([label]) is expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("urgent", Priority.HIGH),
        ("critical", Priority.HIGH),
        ("med", Priority.MEDIUM),
        ("moderate", Priority.MEDIUM),
        ("minor", Priority.LOW),
    ],
)
def test_common_synonyms(label: str, expected: Priority) -> None:
    assert parse_priority([label]) is expected


def test_no_labels_at_all_is_unset() -> None:
    assert parse_priority([]) is Priority.UNSET


@pytest.mark.parametrize(
    "label",
    ["backend", "bug", "good first issue", "documentation", "", "   ", "highlander", "lowercase"],
)
def test_labels_that_say_nothing_about_priority_are_unset(label: str) -> None:
    assert parse_priority([label]) is Priority.UNSET


def test_priority_is_found_among_unrelated_labels() -> None:
    assert parse_priority(["backend", "priority: low", "needs docs"]) is Priority.LOW


def test_the_highest_priority_wins() -> None:
    """A mislabelled item should be escalated rather than buried."""
    assert parse_priority(["low", "HIGH", "medium"]) is Priority.HIGH
    assert parse_priority(["low", "medium"]) is Priority.MEDIUM


def test_a_word_that_merely_contains_a_priority_is_not_one() -> None:
    assert parse_priority(["highlight"]) is Priority.UNSET
    assert parse_priority(["slowdown"]) is Priority.UNSET


def test_an_unknown_word_after_a_priority_prefix_is_unset() -> None:
    assert parse_priority(["priority: whenever"]) is Priority.UNSET
