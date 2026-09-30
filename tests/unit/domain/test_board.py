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

# The columns a command can actually put a card in, on that same board. `In progress` is absent
# and that is the whole reason this argument exists: both it and `In review` read as IN_REVIEW,
# the exact-name pass picks `In review`, and so no status writes to `In progress` at all.
REACHABLE = ("Backlog", "Ready", "In review", "Done")


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
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm=frm, to=to) == ()

    def test_a_column_nothing_can_be_written_to_is_not_a_step(self) -> None:
        """Without this the rule brings the board to a halt, and the board is GitHub's own
        default template. `In progress` and `In review` both read as IN_REVIEW, the exact-name
        pass picks `In review`, so no status writes to `In progress` - and counting it as a step
        made `Ready -> In review` a skip whose remedy was a move nobody could make. A card in
        `Ready` had no forward move left at all."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm="Ready", to="In review") == ()

    def test_the_merge_gate_survives_the_filter(self) -> None:
        """The thing that must NOT be filtered away. `Ready -> Done` jumps over `In review`,
        which is reachable, so it is still refused - and that refusal is the retired
        READY_FOR_MERGE rule, read off the board instead of named in Python."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm="Ready", to="Done") == (
            "In review",
        )

    def test_an_unreachable_column_is_left_out_of_what_was_skipped(self) -> None:
        """`Backlog -> Done` crosses three columns and only two of them are somewhere a
        command could put the card. Naming the third would tell somebody to move it where
        nothing can."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm="Backlog", to="Done") == (
            "Ready",
            "In review",
        )

    def test_a_card_parked_in_an_unreachable_column_can_still_move_on(self) -> None:
        """Somebody dragged it to `In progress` by hand. It is a real place a card sits even
        though no command can put one there, so it has to be a place one can leave."""
        assert (
            must_pass_through(TEMPLATE, reachable=REACHABLE, frm="In progress", to="In review")
            == ()
        )

    def test_the_step_the_old_rule_protected_is_still_one_step(self) -> None:
        """`In review -> Done` is what the retired READY_FOR_MERGE gate existed to require, and it
        is allowed here for the same reason it was there: a reviewer has had their say."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm="In review", to="Done") == ()

    @pytest.mark.parametrize(
        ("frm", "to", "over"),
        [
            ("Backlog", "In progress", ("Ready",)),
            ("Backlog", "Done", ("Ready", "In review")),
            ("Ready", "Done", ("In review",)),
            ("In progress", "Done", ("In review",)),
        ],
    )
    def test_skipping_ahead_names_what_was_skipped(
        self, frm: str, to: str, over: tuple[str, ...]
    ) -> None:
        """The names come back rather than a bool, because the refusal has to say which column to
        move it to instead. A bool would leave whoever ran it to read the board and work it out."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm=frm, to=to) == over

    def test_the_names_are_the_boards_own_spelling(self) -> None:
        """Not the normalised form. They go straight into a sentence somebody reads."""
        assert must_pass_through(
            ("backlog", "READY", "In-Progress"),
            reachable=("backlog", "READY", "In-Progress"),
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
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm=frm, to=to) == ()

    def test_the_same_column_is_allowed(self) -> None:
        """A repeat. `/status` is how a lock that failed on its own gets tried again, so a move to
        where the card already is must not be refused."""
        assert (
            must_pass_through(TEMPLATE, reachable=REACHABLE, frm="In review", to="In review") == ()
        )

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
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm=frm, to=to) == ()

    @pytest.mark.parametrize("frm", [None, "", "   "])
    def test_an_item_whose_column_nobody_recorded_is_allowed(self, frm: str | None) -> None:
        """Every item before its board is first polled. There is nothing to measure from."""
        assert must_pass_through(TEMPLATE, reachable=REACHABLE, frm=frm, to="Done") == ()

    def test_a_board_whose_columns_could_not_be_read_allows_everything(self) -> None:
        """The empty tuple reaches here when the board answered nothing usable."""
        assert must_pass_through((), reachable=(), frm="Backlog", to="Done") == ()

    def test_a_board_with_one_column_allows_the_only_move_there_is(self) -> None:
        assert must_pass_through(("Done",), reachable=("Done",), frm="Done", to="Done") == ()

    def test_two_columns_that_normalise_alike_do_not_reorder_the_board(self) -> None:
        """First wins. A board carrying `In progress` and `in-progress` normalises both to one
        key, and letting the later one win would move the column's index rightwards and start
        refusing moves that are one step apart on the board somebody is looking at."""
        columns = ("Backlog", "In progress", "in-progress", "Done")

        assert must_pass_through(columns, reachable=columns, frm="Backlog", to="In progress") == ()
        assert must_pass_through(columns, reachable=columns, frm="In progress", to="Done") == (
            "in-progress",
        )
