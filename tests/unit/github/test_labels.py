"""Which labels an item should lose and gain to reach a status or a priority.

The hard half is removal, not addition. Priority has been read off whatever spelling a
repository already uses since MVP 2, so an item can be carrying `urgent` or `P1`, and a change
that only writes the new label leaves the old one behind saying something else.
"""

from __future__ import annotations

import pytest

from shannon.domain.enums import Priority, Status
from shannon.github import labels


class TestStatus:
    def test_a_fresh_item_only_gains_a_label(self) -> None:
        change = labels.status_change(["backend"], Status.IN_REVIEW)

        assert change.remove == ()
        assert change.add == "IN_REVIEW"

    def test_moving_status_takes_the_old_one_off(self) -> None:
        """The requirements spell this one out: BACKLOG then NOT_REVIEWED removes the first."""
        change = labels.status_change(["BACKLOG", "backend"], Status.NOT_REVIEWED)

        assert change.remove == ("BACKLOG",)
        assert change.add == "NOT_REVIEWED"

    def test_setting_the_status_it_already_has_does_nothing(self) -> None:
        change = labels.status_change(["BACKLOG"], Status.BACKLOG)

        assert change.nothing_to_do

    def test_unrelated_labels_are_never_touched(self) -> None:
        change = labels.status_change(["bug", "good first issue", "P1"], Status.DONE)

        assert change.remove == ()

    def test_a_status_label_is_matched_whatever_its_case(self) -> None:
        change = labels.status_change(["in_review"], Status.DONE)

        assert change.remove == ("in_review",)

    def test_two_status_labels_at_once_both_come_off(self) -> None:
        """Somebody labelling by hand can leave an item in two states. Only one survives."""
        change = labels.status_change(["BACKLOG", "IN_REVIEW"], Status.DONE)

        assert sorted(change.remove) == ["BACKLOG", "IN_REVIEW"]
        assert change.add == "DONE"

    def test_a_status_word_used_loosely_is_not_a_status(self) -> None:
        """Unlike priority, no synonyms. A repository is free to have a label called `blocked`
        or `review` meaning its own thing, and reading those as workflow states would move
        items through a process nobody asked for."""
        assert labels.status_of(["review", "blocked", "done-ish"]) is None


class TestPriority:
    def test_a_fresh_item_only_gains_a_label(self) -> None:
        change = labels.priority_change(["backend"], Priority.HIGH)

        assert change.remove == ()
        assert change.add == "HIGH"

    @pytest.mark.parametrize(
        "existing", ["urgent", "p-high", "priority: high", "HIGH_PRIORITY", "critical"]
    )
    def test_every_spelling_the_parser_reads_is_a_spelling_it_removes(self, existing: str) -> None:
        """The parser accepts these, so leaving one behind means the item still reads HIGH."""
        change = labels.priority_change([existing], Priority.LOW)

        assert change.remove == (existing,)
        assert change.add == "LOW"

    def test_setting_the_priority_it_already_has_does_nothing(self) -> None:
        change = labels.priority_change(["HIGH"], Priority.HIGH)

        assert change.nothing_to_do

    def test_the_same_priority_spelled_differently_is_still_rewritten(self) -> None:
        """`urgent` reads as HIGH, so nothing changes for a reader, but it leaves two ways of
        saying one thing on the item. The canonical label replaces it."""
        change = labels.priority_change(["urgent"], Priority.HIGH)

        assert change.remove == ("urgent",)
        assert change.add == "HIGH"

    @pytest.mark.parametrize("existing", ["high", "High", "HIGH", "  high  "])
    def test_the_priority_it_already_has_is_left_alone_whatever_its_case(
        self, existing: str
    ) -> None:
        """GitHub's own stock labels are lowercase, and it matches a label name without regard to
        case. Reading `high` as stale purely for its case took it off and put `HIGH` on, which
        re-attached the same label, so the item still read `high` and the next run of the command
        did it all again and answered "is now HIGH priority" every time.
        """
        change = labels.priority_change([existing], Priority.HIGH)

        assert change.nothing_to_do, f"{existing!r} was rewritten for its case alone"

    def test_a_differently_cased_label_still_comes_off_for_a_different_priority(self) -> None:
        """The case rule must not swallow the reason this function exists."""
        change = labels.priority_change(["high"], Priority.LOW)

        assert change.remove == ("high",)
        assert change.add == "LOW"

    def test_status_labels_are_left_alone(self) -> None:
        change = labels.priority_change(["IN_REVIEW", "low"], Priority.HIGH)

        assert change.remove == ("low",)
