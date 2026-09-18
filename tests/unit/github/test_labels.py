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

    def test_tidying_a_stale_label_is_something_to_do(self) -> None:
        """An item wearing two statuses, asked for the one it already has. There is nothing to
        add and a label to take off, and reading that as nothing to do leaves the stale one on
        for ever: `set_status` returns early on `nothing_to_do` and never reaches the write.

        Every other assertion about this property is that it is true, so nothing said what it
        means for it to be false, and `not remove and not add` could be an `or` unnoticed.
        """
        change = labels.status_change(["DONE", "BACKLOG"], Status.DONE)

        assert change.remove == ("BACKLOG",)
        assert change.add == ""
        assert not change.nothing_to_do, "a label left to remove is not nothing to do"

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


class TestWhatThisBotAlreadyOwns:
    """Issue #104. A label somebody may not set by hand, and why each one is on the list.

    The set is wider than the eight names written here, and that is the point: it is built out of
    the two classifiers the rest of this module reads with, so it cannot drift from what they say.
    """

    @pytest.mark.parametrize("name", ["BACKLOG", "IN_REVIEW", "READY_FOR_MERGE", "DONE"])
    def test_a_status_is_reserved(self, name: str) -> None:
        assert labels.reserved_as(name) is labels.status_of([name])

    @pytest.mark.parametrize("name", ["done", "Backlog", "in_review"])
    def test_case_does_not_get_round_it(self, name: str) -> None:
        """`status_of` casefolds, so a lowercase spelling is the same label to GitHub."""
        assert isinstance(labels.reserved_as(name), Status)

    @pytest.mark.parametrize(
        "name", ["high", "urgent", "critical", "medium", "med", "moderate", "low", "minor"]
    )
    def test_every_word_the_priority_parser_reads_is_reserved(self, name: str) -> None:
        """The sharp half. These are ordinary-looking triage words, and writing one would change
        the item's stored priority from a command that never mentioned priority."""
        assert isinstance(labels.reserved_as(name), Priority)

    @pytest.mark.parametrize("name", ["p-high", "prio: low", "priority/med", "HIGH_PRIORITY"])
    def test_the_prefixed_and_suffixed_forms_too(self, name: str) -> None:
        assert isinstance(labels.reserved_as(name), Priority)

    @pytest.mark.parametrize("name", ["good first issue", "bug", "documentation", "help wanted"])
    def test_an_ordinary_label_is_not(self, name: str) -> None:
        """The issue's own examples. If any of these were reserved the feature would be useless."""
        assert labels.reserved_as(name) is None

    def test_unset_is_not_a_reservation(self) -> None:
        """`parse_priority` answers UNSET for everything it does not recognise, and UNSET is the
        absence of a priority rather than one of them."""
        assert labels.reserved_as("wontfix") is None


class TestMovingAnOrdinaryLabel:
    def test_putting_one_on(self) -> None:
        change = labels.label_change(["bug"], "documentation", adding=True)

        assert change.add == "documentation"
        assert change.remove == ()

    def test_nothing_comes_off_to_make_room(self) -> None:
        """Unlike a status or a priority, which are single-valued. An item may carry as many
        ordinary labels as somebody finds useful."""
        change = labels.label_change(["bug", "HIGH", "IN_REVIEW"], "documentation", adding=True)

        assert change.remove == ()

    def test_one_the_item_already_has_is_no_change(self) -> None:
        change = labels.label_change(["bug"], "bug", adding=True)

        assert change.nothing_to_do is True

    def test_case_does_not_make_a_second_label(self) -> None:
        """GitHub matches a label name without regard to case, so writing `Bug` onto an item
        holding `bug` re-attaches what was there and answers as though something happened."""
        change = labels.label_change(["bug"], "Bug", adding=True)

        assert change.nothing_to_do is True

    def test_taking_one_off(self) -> None:
        change = labels.label_change(["bug", "documentation"], "bug", adding=False)

        assert change.remove == ("bug",)
        assert change.add == ""

    def test_taking_off_one_that_is_not_there(self) -> None:
        change = labels.label_change(["documentation"], "bug", adding=False)

        assert change.nothing_to_do is True

    def test_taking_one_off_ignores_case_too(self) -> None:
        change = labels.label_change(["Bug"], "bug", adding=False)

        assert change.remove == ("bug",)
