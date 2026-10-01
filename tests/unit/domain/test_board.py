"""Reading a project board's column back as one of our statuses.

Deliberately more forgiving than the label matcher, and the tests say why: a label namespace is
shared with whatever else a repository labels things, so `done` there may mean anything. A Status
column is a small set somebody chose to describe this workflow, so a board that says `In Progress`
means the thing this bot calls IN_REVIEW.
"""

from __future__ import annotations

import pytest

from shannon.domain.board import must_pass_through, status_from_column
from shannon.domain.enums import Status


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("Backlog", Status.BACKLOG),
        ("Not reviewed", Status.NOT_REVIEWED),
        ("In review", Status.IN_REVIEW),
        ("Done", Status.DONE),
    ],
)
def test_a_board_spelled_our_way_needs_no_translation(column: str, expected: Status) -> None:
    """The four names this bot uses read like board columns because that is what they are,
    so a board named after them costs no configuration at all."""
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


# GitHub's own default template, in the order it creates them. The order is the whole point: it is
# the only thing on a board that says which move comes after which, because Projects v2 has no
# transition rule to read and a single-select option carries no position of its own.
TEMPLATE = ("Backlog", "Ready", "In progress", "In review", "Done")


class TestWhatAMoveHasToPassThrough:
    """Forward one column at a time, backwards as far as you like.

    The asymmetry is deliberate. Sending work back for rework is ordinary; declaring it finished
    early is the thing worth stopping, and it is the thing a status called `Ready for merge` used
    to stop by being named in Python. This reads the same requirement off the board instead.
    """

    @pytest.mark.parametrize(
        ("frm", "to"),
        [
            ("Backlog", "Ready"),
            ("Ready", "In progress"),
            ("In progress", "In review"),
            ("In review", "Done"),
        ],
    )
    def test_one_column_forward_is_allowed(self, frm: str, to: str) -> None:
        assert must_pass_through(TEMPLATE, frm=frm, to=to) == ()

    def test_every_column_counts_as_a_step(self) -> None:
        """`In progress` counts, and for a while it could not. While `/status` took a fixed
        list of four names, nothing could write to that column - it and `In review` both read as
        IN_REVIEW and the exact-name pass takes `In review` - so counting it as a step demanded a
        move nobody could make, and a card in `Ready` had no way forward at all. That needed a
        filter over which columns were writable.

        The picker offers the board's own columns now, so `In progress` can be picked and the
        filter is gone. `Ready -> In review` is a skip again, and this time the remedy exists."""
        assert must_pass_through(TEMPLATE, frm="Ready", to="In review") == ("In progress",)

    def test_the_merge_gate_still_holds(self) -> None:
        """`Ready -> Done` jumps over two columns and is refused, which is the retired
        READY_FOR_MERGE rule read off the board instead of named in Python."""
        assert must_pass_through(TEMPLATE, frm="Ready", to="Done") == (
            "In progress",
            "In review",
        )

    def test_a_card_a_person_dragged_can_still_move_on(self) -> None:
        """Somebody dragged it to `In progress` themselves rather than picking it. One step
        forward from there is `In review`, exactly as it would be either way - where a card came
        from is not a question this asks."""
        assert must_pass_through(TEMPLATE, frm="In progress", to="In review") == ()

    def test_the_step_the_old_rule_protected_is_still_one_step(self) -> None:
        """`In review -> Done` is what the retired READY_FOR_MERGE gate existed to require, and it
        is allowed here for the same reason it was there: a reviewer has had their say."""
        assert must_pass_through(TEMPLATE, frm="In review", to="Done") == ()

    @pytest.mark.parametrize(
        ("frm", "to", "over"),
        [
            ("Backlog", "In progress", ("Ready",)),
            ("Backlog", "Done", ("Ready", "In progress", "In review")),
            ("Ready", "Done", ("In progress", "In review")),
            ("In progress", "Done", ("In review",)),
        ],
    )
    def test_skipping_ahead_names_what_was_skipped(
        self, frm: str, to: str, over: tuple[str, ...]
    ) -> None:
        """The names come back rather than a bool, because the refusal has to say which column to
        move it to instead. A bool would leave whoever ran it to read the board and work it out."""
        assert must_pass_through(TEMPLATE, frm=frm, to=to) == over

    def test_the_names_are_the_boards_own_spelling(self) -> None:
        """Not the normalised form. They go straight into a sentence somebody reads."""
        assert must_pass_through(
            ("backlog", "READY", "In-Progress"),
            frm="backlog",
            to="in progress",
        ) == ("READY",)

    @pytest.mark.parametrize(
        ("frm", "to"),
        [
            ("Done", "In review"),
            ("Done", "Backlog"),
            ("In review", "Ready"),
            ("In progress", "Backlog"),
        ],
    )
    def test_backwards_is_always_allowed_however_far(self, frm: str, to: str) -> None:
        """`Done -> Backlog` crosses three columns and is allowed. Reopening something is not a
        skipped step, and making somebody walk a card back one column at a time would be a rule
        about bookkeeping rather than about review."""
        assert must_pass_through(TEMPLATE, frm=frm, to=to) == ()

    def test_the_same_column_is_allowed(self) -> None:
        """A repeat. `/status` is how a lock that failed on its own gets tried again, so a move to
        where the card already is must not be refused."""
        assert must_pass_through(TEMPLATE, frm="In review", to="In review") == ()

    @pytest.mark.parametrize(
        ("frm", "to"),
        [
            ("Needs design input", "Done"),
            ("Backlog", "Needs design input"),
            ("Needs design input", "Somewhere else"),
        ],
    )
    def test_a_column_not_on_this_board_cannot_be_reasoned_about(self, frm: str, to: str) -> None:
        """Fails open, and has to. The rule is derived from a list this bot did not write, so a
        board somebody renamed one column on would otherwise refuse every move on it until it was
        renamed back."""
        assert must_pass_through(TEMPLATE, frm=frm, to=to) == ()

    @pytest.mark.parametrize("frm", [None, "", "   "])
    def test_an_item_whose_column_nobody_recorded_is_allowed(self, frm: str | None) -> None:
        """Every item before its board is first polled. There is nothing to measure from."""
        assert must_pass_through(TEMPLATE, frm=frm, to="Done") == ()

    def test_a_board_whose_columns_could_not_be_read_allows_everything(self) -> None:
        """The empty tuple reaches here when the board answered nothing usable."""
        assert must_pass_through((), frm="Backlog", to="Done") == ()

    def test_a_board_with_one_column_allows_the_only_move_there_is(self) -> None:
        assert must_pass_through(("Done",), frm="Done", to="Done") == ()

    def test_two_columns_that_normalise_alike_do_not_reorder_the_board(self) -> None:
        """First wins. A board carrying `In progress` and `in-progress` normalises both to one
        key, and letting the later one win would move the column's index rightwards and start
        refusing moves that are one step apart on the board somebody is looking at."""
        columns = ("Backlog", "In progress", "in-progress", "Done")

        assert must_pass_through(columns, frm="Backlog", to="In progress") == ()
        assert must_pass_through(columns, frm="In progress", to="Done") == ("in-progress",)
