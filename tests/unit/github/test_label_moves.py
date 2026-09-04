"""Which label a delivery moved, read off the delivery rather than worked out.

The metadata block lists every label an item has and is rewritten on every event, so nothing
needed to know which one changed until the thread had to say so out loud. GitHub already knows:
it sends one delivery per label and names that label at the top level.
"""

from __future__ import annotations

import pytest

from shannon.github.webhooks.labels import LABEL_ACTIONS, parse_label_move

pytestmark = pytest.mark.unit


def a_delivery(name: str = "bug") -> dict:
    return {"label": {"name": name, "color": "d73a4a"}, "issue": {"number": 7}}


def test_a_label_going_on_is_read_as_added() -> None:
    move = parse_label_move("labeled", a_delivery("high priority"))

    assert move is not None
    assert move.name == "high priority"
    assert move.added is True


def test_a_label_coming_off_is_read_as_removed() -> None:
    move = parse_label_move("unlabeled", a_delivery("high priority"))

    assert move is not None
    assert move.added is False


def test_the_two_actions_that_name_a_label_are_the_only_ones_read() -> None:
    """Every other action carries the whole label list and nothing about what changed, so there
    is no move in it to announce. `opened` on an item created with four labels is the case: it
    is one delivery, four labels, and nothing moved."""
    assert set(LABEL_ACTIONS) == {"labeled", "unlabeled"}
    for action in ("opened", "edited", "closed", "reopened", "assigned", "unassigned"):
        assert parse_label_move(action, a_delivery()) is None, action


@pytest.mark.parametrize(
    "label",
    [None, "bug", [], {}, {"name": ""}, {"name": 7}, {"color": "d73a4a"}],
    ids=["missing", "a string", "a list", "empty", "empty name", "a number", "no name"],
)
def test_a_delivery_that_says_nothing_usable_about_a_label_is_not_announced(label: object) -> None:
    """None rather than raising, because this decides whether to say something and nothing else.
    The item sync still handles the delivery in full; only the line is lost."""
    payload = {"issue": {"number": 7}}
    if label is not None:
        payload["label"] = label

    assert parse_label_move("labeled", payload) is None
