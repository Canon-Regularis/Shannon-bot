"""What a status or a priority is called in front of a person. Issue #147.

`Status.value` and `Priority.value` are what `tracked_items` stores and what the GitHub labels were
called before issue #246, and neither is a sentence. So the display form is a separate table, and
what these hold is that it stays separate.

Since #246 the spoken form is also the column a status label names where no board names one -
`status: In review` - so the round trip below is the one worth reading: the label this bot writes
for a status has to read back as that status and never another.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.safe_text import EMPTY
from shannon.domain.board import _COLUMNS, columns_for, normalise
from shannon.domain.enums import Priority, Status, spoken
from shannon.domain.priority import parse_priority
from shannon.github.labels import PRIORITY_LABELS, own_column, status_label, status_of

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
def test_the_label_for_a_status_reads_back_as_it(status: Status) -> None:
    """The label this bot writes for a status where no board names the column."""
    assert status_of([status_label(own_column(status)).name]) is status


@pytest.mark.parametrize("status", list(Status), ids=str)
def test_a_spoken_status_on_its_own_never_reads_back_as_a_different_one(status: Status) -> None:
    """Without the prefix, the spoken form is not a status label at all - except `Backlog` and
    `Done`, which case-fold to the labels this bot used to write and which GitHub would have
    matched to `BACKLOG` and `DONE` anyway. What must never happen is one status's display form
    reading back as another status, which would move an item somebody else's command had set."""
    assert status_of([spoken(status)]) in (None, status)


@pytest.mark.parametrize("priority", list(PRIORITY_LABELS), ids=str)
def test_a_spoken_priority_is_not_a_label_this_bot_would_write(priority: Priority) -> None:
    """The label says what it is, `priority: High`, where the word alone is only `High`."""
    assert spoken(priority) != PRIORITY_LABELS[priority].name


@pytest.mark.parametrize("priority", list(PRIORITY_LABELS), ids=str)
def test_the_label_for_a_priority_reads_back_as_it(priority: Priority) -> None:
    assert parse_priority([PRIORITY_LABELS[priority].name]) is priority


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
