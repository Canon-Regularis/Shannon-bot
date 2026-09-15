"""Which label a delivery moved, read off the delivery rather than worked out.

The metadata block lists every label an item has and is rewritten on every event, so nothing
needed to know which one changed until the thread had to say so out loud. GitHub already knows:
it sends one delivery per label and names that label at the top level.
"""

from __future__ import annotations

import pytest

from shannon.domain.enums import Priority, Status
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


class TestWhatTheLabelMeans:
    """Classified where the move is built, so nothing downstream has to ask twice and get a
    different answer from the one the delivery was read with."""

    def test_our_own_spelling_of_a_priority_is_carried_as_one(self) -> None:
        move = parse_label_move("labeled", a_delivery("HIGH"))

        assert move is not None
        assert move.priority is Priority.HIGH
        assert move.status is None

    @pytest.mark.parametrize(
        ("name", "level"),
        [
            ("urgent", Priority.HIGH),
            ("critical", Priority.HIGH),
            ("priority: medium", Priority.MEDIUM),
            ("p-low", Priority.LOW),
            ("HIGH_PRIORITY", Priority.HIGH),
        ],
    )
    def test_whatever_the_repository_already_spells_it_is_read_as_one(
        self, name: str, level: Priority
    ) -> None:
        """Priority has been read off whatever spelling a repository uses since MVP 2, and the
        line announcing one has to agree with the `Priority:` field above it or the two say
        different things about the same label."""
        move = parse_label_move("labeled", a_delivery(name))

        assert move is not None
        assert move.priority is level

    def test_a_priority_coming_off_still_says_which_one(self) -> None:
        """Read off the name, which does not change with the direction. A line about a cleared
        priority has to know it is about a priority before it can say so."""
        move = parse_label_move("unlabeled", a_delivery("urgent"))

        assert move is not None
        assert move.priority is Priority.HIGH
        assert move.added is False

    @pytest.mark.parametrize("status", list(Status))
    def test_each_status_is_carried_as_one(self, status: Status) -> None:
        move = parse_label_move("labeled", a_delivery(status.value))

        assert move is not None
        assert move.status is status
        assert move.priority is Priority.UNSET

    def test_an_ordinary_label_is_neither(self) -> None:
        move = parse_label_move("labeled", a_delivery("good first issue"))

        assert move is not None
        assert move.priority is Priority.UNSET
        assert move.status is None

    @pytest.mark.parametrize("status", list(Status))
    def test_the_two_groups_cannot_both_claim_a_label(self, status: Status) -> None:
        """The renderer tests one group and then the other, so this says that order is a
        formality rather than a precedence rule nobody wrote down. Numbered priority schemes are
        deliberately unread, which is what keeps a `P1` out of both groups as well."""
        move = parse_label_move("labeled", a_delivery(status.value))

        assert move is not None
        assert move.priority is Priority.UNSET, f"{status.value} was read as a priority too"
