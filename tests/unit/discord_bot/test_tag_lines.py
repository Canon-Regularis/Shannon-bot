"""What a thread says when a tag moves, split by what the tag means.

The line exists because a Discord edit is silent: it posts no message, notifies nobody and does
not bump the thread, so tagging an item moved the block and looked from the channel exactly like
nothing happening. Three groups rather than one, because two of them are labels this bot writes
itself and saying the same sentence about all three buried the ones that matter.

The exact strings are asserted rather than a substring of them, because these are the whole of
what a reader sees and a mark quietly changing is the failure this file is for.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_label_change
from shannon.domain.enums import Priority, Status
from shannon.domain.models import LabelMove

pytestmark = pytest.mark.unit


class TestAPriorityLabel:
    """Coloured by level, so how urgent reads before which label has been read."""

    @pytest.mark.parametrize(
        ("level", "mark"),
        [(Priority.HIGH, "🔴"), (Priority.MEDIUM, "🟠"), (Priority.LOW, "🟢")],
    )
    def test_each_level_has_its_own_colour(self, level: Priority, mark: str) -> None:
        move = LabelMove(name=level.value, added=True, priority=level)

        assert format_label_change(move) == f"{mark} **Priority set:** `{level.value}`"

    def test_whatever_the_repository_spells_it_is_coloured_by_what_it_means(self) -> None:
        """`urgent` is HIGH to the parser, so it is red here, and the label is still named as
        the repository writes it rather than translated into ours."""
        move = LabelMove(name="urgent", added=True, priority=Priority.HIGH)

        assert format_label_change(move) == "🔴 **Priority set:** `urgent`"

    def test_one_coming_off_has_no_colour_to_carry(self) -> None:
        """What a cleared priority leaves behind is not a level, so it is not given one."""
        move = LabelMove(name="urgent", added=False, priority=Priority.HIGH)

        assert format_label_change(move) == "⚪ **Priority cleared:** `urgent`"


class TestAStatusLabel:
    def test_one_going_on_is_a_status_set(self) -> None:
        move = LabelMove(name="IN_REVIEW", added=True, status=Status.IN_REVIEW)

        assert format_label_change(move) == "📋 **Status set:** `IN_REVIEW`"

    def test_one_coming_off_is_a_status_cleared(self) -> None:
        move = LabelMove(name="BACKLOG", added=False, status=Status.BACKLOG)

        assert format_label_change(move) == "📋 **Status cleared:** `BACKLOG`"


class TestAnOrdinaryLabel:
    def test_one_going_on_says_added(self) -> None:
        assert format_label_change(LabelMove(name="bug", added=True)) == "🏷️ Tag `bug` added."

    def test_one_coming_off_says_removed(self) -> None:
        assert format_label_change(LabelMove(name="bug", added=False)) == "🏷️ Tag `bug` removed."


class TestALabelNameNobodyHereChose:
    """A label is named by anybody with triage rights on the repository, so every group treats
    it as untrusted. This was a real hole in the metadata block once and is worth pinning on
    each of the three paths rather than on the one that happened to be written first."""

    def test_a_mention_cannot_ping_from_a_priority_line(self) -> None:
        move = LabelMove(name="<@1234>", added=True, priority=Priority.HIGH)

        assert "<@1234>" not in format_label_change(move)

    def test_a_mention_cannot_ping_from_a_status_line(self) -> None:
        move = LabelMove(name="<@1234>", added=True, status=Status.DONE)

        assert "<@1234>" not in format_label_change(move)

    def test_backticks_cannot_break_out_of_an_ordinary_tag_line(self) -> None:
        line = format_label_change(LabelMove(name="a`b", added=True))

        assert line == "🏷️ Tag ``a`b`` added."
