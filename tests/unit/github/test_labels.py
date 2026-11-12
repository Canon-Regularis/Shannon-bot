"""Which labels an item should lose and gain to reach a status or a priority, and how each looks.

The hard half is removal, not addition. Priority has been read off whatever spelling a
repository already uses since MVP 2, so an item can be carrying `urgent` or `P1`, and a change
that only writes the new label leaves the old one behind saying something else. Since issue #246
the labels this bot used to write - `HIGH`, `IN_REVIEW` - are one more spelling to take off.
"""

from __future__ import annotations

import re

import pytest

from shannon.domain.enums import Priority, Status
from shannon.github import labels
from shannon.github.projects import BoardColumn

IN_REVIEW = labels.status_label("In review")
DONE = labels.status_label("Done")


class TestTheNames:
    """Issue #246's own words: `priority: High`, `priority: Medium` and `priority: Low`, in red,
    amber and yellow, and `status: ` and the column."""

    @pytest.mark.parametrize(
        ("priority", "name", "colour"),
        [
            (Priority.HIGH, "priority: High", "d73a4a"),
            (Priority.MEDIUM, "priority: Medium", "f59e0b"),
            (Priority.LOW, "priority: Low", "fde047"),
        ],
    )
    def test_each_priority_is_named_and_coloured(
        self, priority: Priority, name: str, colour: str
    ) -> None:
        assert labels.PRIORITY_LABELS[priority].name == name
        assert labels.PRIORITY_LABELS[priority].colour == colour

    @pytest.mark.parametrize("priority", list(labels.PRIORITY_LABELS), ids=str)
    def test_each_priority_takes_over_the_label_this_bot_used_to_write(
        self, priority: Priority
    ) -> None:
        assert labels.PRIORITY_LABELS[priority].replaces == (priority.value,)

    def test_unset_has_no_label(self) -> None:
        """UNSET is the absence of a priority, so there is nothing to write for it."""
        assert Priority.UNSET not in labels.PRIORITY_LABELS

    def test_a_status_label_names_its_column(self) -> None:
        assert labels.status_label("Ready To Merge").name == "status: Ready To Merge"

    def test_the_column_is_written_as_given_without_its_padding(self) -> None:
        assert labels.status_label("  In progress ").name == "status: In progress"

    @pytest.mark.parametrize(
        "colour",
        [
            *labels.BOARD_COLOURS.values(),
            *labels.STATUS_COLOURS.values(),
            labels.UNKNOWN_STATUS_COLOUR,
            *(definition.colour for definition in labels.PRIORITY_LABELS.values()),
        ],
    )
    def test_every_colour_is_how_github_takes_one(self, colour: str) -> None:
        """Six hex digits and no `#`: GitHub refuses anything else with a 422."""
        assert re.fullmatch(r"[0-9a-f]{6}", colour)

    def test_every_status_has_a_colour_of_its_own(self) -> None:
        assert set(labels.STATUS_COLOURS) == set(Status)
        assert len(set(labels.STATUS_COLOURS.values())) == len(Status)


class TestAStatusLabelsColour:
    def test_the_boards_colour_wins(self) -> None:
        assert labels.status_label("In review", board_colour="ORANGE").colour == "fb8500"

    def test_the_boards_colour_is_read_whatever_its_case(self) -> None:
        assert labels.status_label("Done", board_colour="pink").colour == "e85aad"

    @pytest.mark.parametrize("given", [None, "", "TEAL"])
    def test_a_status_falls_back_on_its_own_colour(self, given: str | None) -> None:
        """No board, a column with no colour, or one GitHub has added since this was written."""
        assert (
            labels.status_label("In review", board_colour=given).colour
            == (labels.STATUS_COLOURS[Status.IN_REVIEW])
        )

    def test_a_column_with_no_status_or_colour_is_grey(self) -> None:
        assert labels.status_label("Ready To Merge").colour == labels.UNKNOWN_STATUS_COLOUR


class TestWhatAStatusLabelReplaces:
    @pytest.mark.parametrize("status", list(Status), ids=str)
    def test_a_status_column_takes_over_the_old_label_for_its_status(self, status: Status) -> None:
        assert labels.status_label(labels.own_column(status)).replaces == (status.value,)

    def test_a_column_named_differently_takes_over_the_same_one(self) -> None:
        """`In progress` reads as IN_REVIEW, so it is the label that used to mean it."""
        assert labels.status_label("In progress").replaces == ("IN_REVIEW",)

    def test_a_column_with_no_status_takes_over_nothing(self) -> None:
        """There is no telling which of the old labels meant `Ready To Merge`."""
        assert labels.status_label("Ready To Merge").replaces == ()


TEMPLATE = (
    BoardColumn("Backlog", "GRAY"),
    BoardColumn("Ready", "BLUE"),
    BoardColumn("In progress", "YELLOW"),
    BoardColumn("In review", "PURPLE"),
    BoardColumn("Ready To Merge", "ORANGE"),
    BoardColumn("Done", "GREEN"),
)


class TestTheLabelForAMoveOnABoard:
    """`status_label_on`: the column the card lands in, as the board has it."""

    def test_a_named_column_is_the_boards(self) -> None:
        label = labels.status_label_on(TEMPLATE, Status.IN_REVIEW, "in-progress")

        assert label.definition.name == "status: In progress"
        assert label.definition.colour == "d4a72c"
        assert label.settled

    def test_no_column_named_is_the_boards_column_for_the_status(self) -> None:
        """The column called exactly what this bot calls the status wins over a synonym."""
        label = labels.status_label_on(TEMPLATE, Status.IN_REVIEW)

        assert label.definition.name == "status: In review"

    def test_a_status_named_by_a_word_the_board_lacks_lands_where_the_card_does(self) -> None:
        label = labels.status_label_on(TEMPLATE, Status.DONE, "Shipped")

        assert label.definition.name == "status: Done"

    def test_a_column_with_no_status_is_the_boards(self) -> None:
        label = labels.status_label_on(TEMPLATE, Status.IN_REVIEW, "ready to merge")

        assert label.definition == labels.LabelDefinition("status: Ready To Merge", "fb8500")
        assert label.settled

    def test_a_column_with_no_status_the_board_lacks_is_unsettled(self) -> None:
        label = labels.status_label_on(TEMPLATE, Status.IN_REVIEW, "  Shipping soon ")

        assert label.definition.name == "status: Shipping soon"
        assert not label.settled

    def test_a_status_the_board_has_no_column_for_is_this_bots_own(self) -> None:
        board = (BoardColumn("Todo", "BLUE"), BoardColumn("Done", "GREEN"))

        label = labels.status_label_on(board, Status.BACKLOG)

        assert label.definition == labels.status_label("Backlog")
        assert label.definition.replaces == ("BACKLOG",)
        assert label.settled

    def test_with_no_board_it_is_this_bots_own(self) -> None:
        label = labels.status_label_on((), Status.IN_REVIEW, "In progress")

        assert label.definition == labels.status_label("In review")
        assert label.settled

    @pytest.mark.parametrize(("named", "name"), [("in progress", "in progress"), ("", "In review")])
    def test_a_board_that_will_not_read_gives_the_name_given_unsettled(
        self, named: str, name: str
    ) -> None:
        label = labels.status_label_on(None, Status.IN_REVIEW, named)

        assert label.definition.name == f"status: {name}"
        assert not label.settled

    def test_the_old_label_is_taken_over_where_one_column_claims_it(self) -> None:
        board = (BoardColumn("Todo"), BoardColumn("Doing", "YELLOW"), BoardColumn("Done"))

        label = labels.status_label_on(board, Status.IN_REVIEW, "Doing")

        assert label.definition.replaces == ("IN_REVIEW",)

    @pytest.mark.parametrize("named", ["In progress", "In review"])
    def test_not_where_two_columns_could_claim_it(self, named: str) -> None:
        """Either would relabel every item carrying `IN_REVIEW`, including the ones whose cards
        are in the other column."""
        label = labels.status_label_on(TEMPLATE, Status.IN_REVIEW, named)

        assert label.definition.replaces == ()

    def test_one_column_for_a_status_beside_two_for_another_still_takes_over(self) -> None:
        label = labels.status_label_on(TEMPLATE, Status.DONE, "Done")

        assert label.definition.replaces == ("DONE",)


class TestStatus:
    def test_a_fresh_item_only_gains_a_label(self) -> None:
        change = labels.status_change(["backend"], IN_REVIEW)

        assert change.remove == ()
        assert change.add == "status: In review"
        assert change.define == IN_REVIEW

    def test_moving_status_takes_the_old_one_off(self) -> None:
        """The requirements spell this one out: BACKLOG then NOT_REVIEWED removes the first."""
        change = labels.status_change(
            ["status: Backlog", "backend"], labels.status_label("Not reviewed")
        )

        assert change.remove == ("status: Backlog",)
        assert change.add == "status: Not reviewed"

    def test_the_label_this_bot_used_to_write_comes_off_too(self) -> None:
        change = labels.status_change(["BACKLOG", "backend"], IN_REVIEW)

        assert change.remove == ("BACKLOG",)
        assert change.add == "status: In review"

    def test_setting_the_status_it_already_has_does_nothing(self) -> None:
        change = labels.status_change(["status: In review"], IN_REVIEW)

        assert change.nothing_to_do
        assert change.define is None, "nothing is being put on, so there is nothing to define"

    def test_the_label_it_already_has_is_left_alone_whatever_its_case(self) -> None:
        """GitHub matches a label name without regard to case."""
        assert labels.status_change(["STATUS: in review "], IN_REVIEW).nothing_to_do

    def test_unrelated_labels_are_never_touched(self) -> None:
        change = labels.status_change(["bug", "good first issue", "P1", "HIGH"], DONE)

        assert change.remove == ()

    def test_an_old_status_label_is_matched_whatever_its_case(self) -> None:
        change = labels.status_change(["in_review"], DONE)

        assert change.remove == ("in_review",)

    def test_two_status_labels_at_once_both_come_off(self) -> None:
        """Somebody labelling by hand can leave an item in two states. Only one survives."""
        change = labels.status_change(["BACKLOG", "status: In review"], DONE)

        assert sorted(change.remove) == ["BACKLOG", "status: In review"]
        assert change.add == "status: Done"

    def test_a_column_with_no_status_is_still_a_status_label_to_take_off(self) -> None:
        change = labels.status_change(["status: Ready To Merge"], DONE)

        assert change.remove == ("status: Ready To Merge",)

    def test_two_columns_for_one_status_are_two_labels(self) -> None:
        """`In progress` and `In review` both read as IN_REVIEW and are different columns, so a
        card dragged from one to the other changes its label."""
        change = labels.status_change(["status: In progress"], IN_REVIEW)

        assert change.remove == ("status: In progress",)
        assert change.add == "status: In review"

    def test_tidying_a_stale_label_is_something_to_do(self) -> None:
        """An item wearing two statuses, asked for the one it already has. There is nothing to
        add and a label to take off, and reading that as nothing to do leaves the stale one on
        for ever: `set_status` returns early on `nothing_to_do` and never reaches the write.

        Every other assertion about this property is that it is true, so nothing said what it
        means for it to be false, and `not remove and not add` could be an `or` unnoticed.
        """
        change = labels.status_change(["status: Done", "BACKLOG"], DONE)

        assert change.remove == ("BACKLOG",)
        assert change.add == ""
        assert not change.nothing_to_do, "a label left to remove is not nothing to do"


class TestReadingAStatusBack:
    def test_a_status_word_used_loosely_is_not_a_status(self) -> None:
        """Unlike priority, no synonyms. A repository is free to have a label called `blocked`
        or `review` meaning its own thing, and reading those as workflow states would move
        items through a process nobody asked for."""
        assert labels.status_of(["review", "blocked", "done-ish"]) is None

    @pytest.mark.parametrize(
        ("name", "status"),
        [
            ("status: In progress", Status.IN_REVIEW),
            ("Status: done", Status.DONE),
            ("IN_REVIEW", Status.IN_REVIEW),
            ("backlog", Status.BACKLOG),
        ],
    )
    def test_both_spellings_are_read(self, name: str, status: Status) -> None:
        assert labels.status_of(["bug", name]) is status

    def test_a_column_with_no_status_reads_as_none_and_is_a_status_label(self) -> None:
        assert labels.status_of(["status: Ready To Merge"]) is None
        assert labels.is_status_label("status: Ready To Merge")

    @pytest.mark.parametrize("name", ["bug", "READY_FOR_MERGE", "statuses", "high"])
    def test_an_ordinary_label_is_not_a_status_label(self, name: str) -> None:
        """`READY_FOR_MERGE` stopped being a status before #246 and stays an ordinary label."""
        assert not labels.is_status_label(name)


class TestPriority:
    def test_a_fresh_item_only_gains_a_label(self) -> None:
        change = labels.priority_change(["backend"], Priority.HIGH)

        assert change.remove == ()
        assert change.add == "priority: High"
        assert change.define == labels.PRIORITY_LABELS[Priority.HIGH]

    @pytest.mark.parametrize(
        "existing", ["urgent", "p-high", "priority: high", "HIGH_PRIORITY", "critical", "HIGH"]
    )
    def test_every_spelling_the_parser_reads_is_a_spelling_it_removes(self, existing: str) -> None:
        """The parser accepts these, so leaving one behind means the item still reads HIGH."""
        change = labels.priority_change([existing], Priority.LOW)

        assert change.remove == (existing,)
        assert change.add == "priority: Low"

    def test_setting_the_priority_it_already_has_does_nothing(self) -> None:
        change = labels.priority_change(["priority: High"], Priority.HIGH)

        assert change.nothing_to_do
        assert change.define is None

    def test_the_label_this_bot_used_to_write_is_rewritten(self) -> None:
        """`HIGH` reads as HIGH, and is the old name all the same. Comparing against the enum's
        spelling, as this did while the two were the same word, would leave it on for ever."""
        change = labels.priority_change(["HIGH"], Priority.HIGH)

        assert change.remove == ("HIGH",)
        assert change.add == "priority: High"

    def test_the_same_priority_spelled_differently_is_still_rewritten(self) -> None:
        """`urgent` reads as HIGH, so nothing changes for a reader, but it leaves two ways of
        saying one thing on the item. The canonical label replaces it."""
        change = labels.priority_change(["urgent"], Priority.HIGH)

        assert change.remove == ("urgent",)
        assert change.add == "priority: High"

    @pytest.mark.parametrize(
        "existing", ["priority: high", "Priority: High", "PRIORITY: HIGH", "  priority: High  "]
    )
    def test_the_priority_it_already_has_is_left_alone_whatever_its_case(
        self, existing: str
    ) -> None:
        """GitHub matches a label name without regard to case. Reading `priority: high` as stale
        purely for its case took it off and put `priority: High` on, which re-attached the same
        label, so the next run of the command did it all again and answered "is now High
        priority" every time.
        """
        change = labels.priority_change([existing], Priority.HIGH)

        assert change.nothing_to_do, f"{existing!r} was rewritten for its case alone"

    def test_repeating_the_command_converges(self) -> None:
        """The bug #246 would have shipped: every run took the label off and put it back."""
        first = labels.priority_change(["bug"], Priority.MEDIUM)
        second = labels.priority_change(["bug", first.add], Priority.MEDIUM)

        assert second.nothing_to_do

    def test_a_differently_cased_label_still_comes_off_for_a_different_priority(self) -> None:
        """The case rule must not swallow the reason this function exists."""
        change = labels.priority_change(["priority: high"], Priority.LOW)

        assert change.remove == ("priority: high",)
        assert change.add == "priority: Low"

    def test_status_labels_are_left_alone(self) -> None:
        """Including one whose column happens to be called a priority."""
        change = labels.priority_change(
            ["IN_REVIEW", "status: High priority", "low"], Priority.HIGH
        )

        assert change.remove == ("low",)


class TestWhatThisBotAlreadyOwns:
    """Issue #104. A label somebody may not set by hand, and why each one is on the list.

    The set is wider than the names written here, and that is the point: it is built out of the
    two classifiers the rest of this module reads with, so it cannot drift from what they say.
    """

    @pytest.mark.parametrize(
        ("name", "column"),
        [
            ("BACKLOG", "Backlog"),
            ("IN_REVIEW", "In review"),
            ("DONE", "Done"),
            ("done", "Done"),
            ("in_review", "In review"),
        ],
    )
    def test_an_old_status_label_is_reserved_as_the_column_to_pick(
        self, name: str, column: str
    ) -> None:
        """`status_of` casefolds, so a lowercase spelling is the same label to GitHub."""
        assert labels.reserved_as(name) == column

    @pytest.mark.parametrize(
        ("name", "column"),
        [("status: Ready To Merge", "Ready To Merge"), ("STATUS:  In review", "In review")],
    )
    def test_a_status_label_is_reserved_as_its_column(self, name: str, column: str) -> None:
        assert labels.reserved_as(name) == column

    def test_a_bare_prefix_is_still_reserved(self) -> None:
        assert labels.reserved_as("status:") == "a column"

    def test_ready_for_merge_is_an_ordinary_label(self) -> None:
        assert labels.reserved_as("READY_FOR_MERGE") is None

    @pytest.mark.parametrize(
        "name", ["high", "urgent", "critical", "medium", "med", "moderate", "low", "minor"]
    )
    def test_every_word_the_priority_parser_reads_is_reserved(self, name: str) -> None:
        """The sharp half. These are ordinary-looking triage words, and writing one would change
        the item's stored priority from a command that never mentioned priority."""
        assert isinstance(labels.reserved_as(name), Priority)

    @pytest.mark.parametrize(
        "name", ["p-high", "prio: low", "priority/med", "HIGH_PRIORITY", "priority: High"]
    )
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
        assert change.define is None, "an ordinary label is the repository's, not this bot's"

    def test_nothing_comes_off_to_make_room(self) -> None:
        """Unlike a status or a priority, which are single-valued. An item may carry as many
        ordinary labels as somebody finds useful."""
        change = labels.label_change(
            ["bug", "priority: High", "status: In review"], "documentation", adding=True
        )

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
