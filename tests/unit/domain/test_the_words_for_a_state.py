"""What a status or a priority is called in front of a person. Issue #147.

`Status.value` and `Priority.value` are three things at once and none of them is a sentence: the
GitHub label name, the varchar in `tracked_items`, and the key a priority label is case-folded
against. So the display form is a separate table, and what these hold is that it stays separate.

The round-trip pair below is the one worth reading. Unifying the two spellings looks like a
tidy-up and would have `/set_ready_for_merge` write a brand new `Ready for merge` label onto the
repository, which `github/labels.py` cannot take off again.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.safe_text import EMPTY
from shannon.domain.board import _COLUMNS, columns_for, normalise
from shannon.domain.enums import Priority, Status, spoken
from shannon.github.labels import PRIORITY_LABELS, STATUS_LABELS, status_of

STATES: list[Status | Priority] = [*Status, *Priority]


@pytest.mark.parametrize("state", STATES, ids=str)
def test_every_state_has_a_word(state: Status | Priority) -> None:
    """A total table, so `spoken` needs no fallback arm and cannot raise on a value somebody
    adds to either enum without looking here."""
    assert spoken(state)


@pytest.mark.parametrize("state", STATES, ids=str)
def test_no_word_is_the_stored_spelling(state: Status | Priority) -> None:
    assert spoken(state) != state.value, "the display form and the stored form are the same string"


@pytest.mark.parametrize("state", STATES, ids=str)
def test_no_word_is_shouted_or_joined_by_underscores(state: Status | Priority) -> None:
    said = spoken(state)
    assert "_" not in said
    assert said != said.upper()
    assert said == said[0].upper() + said[1:], "a reply starts sentences with these"


@pytest.mark.parametrize("status", list(Status), ids=str)
def test_a_spoken_status_never_reads_back_as_a_different_one(status: Status) -> None:
    """The hazard, stated as precisely as it is true.

    `STATUS_LABELS` is `{status: status.value}` and `status_of` reads a label back on the exact
    case-folded spelling. The one-word statuses case-fold to the same key as their label, so
    `Done` reads back as DONE; the multi-word ones do not, because a space is not an underscore.

    Either answer is safe and a third would not be. What must never happen is one status's
    display form reading back as another status, which would move an item somebody else's
    command had set.
    """
    assert spoken(status) != STATUS_LABELS[status]
    assert status_of([spoken(status)]) in (None, status)


@pytest.mark.parametrize("status", [s for s in Status if " " in spoken(s)], ids=str)
def test_a_status_whose_word_has_a_space_would_be_a_label_of_its_own(status: Status) -> None:
    """These are the ones the round trip cannot save, and so the ones worth a test.

    A space is not an underscore, so `Ready for merge` matches no key and `status_change` would
    put it on the repository as a brand new label. Nothing in `github/labels.py` takes a label
    off for good, so that is permanent, which is the argument for `spoken` being a table of its
    own rather than something derived from `.value`.
    """
    assert status_of([spoken(status)]) is None
    assert spoken(status).casefold() != STATUS_LABELS[status].casefold()


@pytest.mark.parametrize("priority", list(PRIORITY_LABELS), ids=str)
def test_a_spoken_priority_is_not_a_label_this_bot_would_write(priority: Priority) -> None:
    """Priority fails differently and worse: `PRIORITY_LABELS` hardcodes its strings, so the
    label added would stay right while the comparison that removes the old one went wrong, and
    an item would carry two priority labels that disagree."""
    assert spoken(priority) != PRIORITY_LABELS[priority]


def test_the_absence_of_a_priority_is_called_what_an_empty_field_is_called() -> None:
    """UNSET is the absence of a priority rather than one of them, which is why no label answers
    to it, and the card already has a word for a field with nothing in it."""
    assert spoken(Priority.UNSET) == EMPTY


def test_every_status_is_named_by_a_column_that_reads_back_as_it() -> None:
    """The suggestion list a board is offered has to be complete for BOTH ways a column is
    matched, and it is only because every status's own spoken form is itself a column name
    meaning that status.

    That is otherwise an accident. Break it and `columns_for` would offer a list missing the one
    name the exact-match pass would have taken - so somebody told "call a column any of these"
    would pick one, and the column they were told to make would be matched by the second pass
    rather than the first, which on a board carrying a synonym too is a different column.
    """
    for status in Status:
        assert normalise(spoken(status)) in columns_for(status), (
            f"{spoken(status)!r} is what the picker calls {status.value}, "
            "and no column by that name reads back as it"
        )


def test_the_suggestions_are_the_whole_table_and_nothing_else() -> None:
    """Derived rather than hand-kept, so a word added to the table cannot go unoffered."""
    offered = {column for status in Status for column in columns_for(status)}

    assert offered == set(_COLUMNS)
