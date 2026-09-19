"""What a thread says when a tag moves, split by what the tag means.

The line exists because a Discord edit is silent: it posts no message, notifies nobody and does
not bump the thread, so tagging an item moved the block and looked from the channel exactly like
nothing happening. Three groups rather than one, because two of them are labels this bot writes
itself and saying the same sentence about all three buried the ones that matter.

The exact strings are asserted rather than a substring of them, because these are the whole of
what a reader sees and a mark quietly changing is the failure this file is for.

Since issue #116 the line is a card with a bar down its side, and the bar is asserted beside the
words for the same reason. A mark and a colour saying different things is worse than either on
its own, and nothing else in the suite would notice.
"""

from __future__ import annotations

import pytest

from shannon.discord_bot.formatting import format_label_change
from shannon.discord_bot.panels import Accent, BlockKind
from shannon.domain.enums import Priority, Status
from shannon.domain.models import LabelMove

pytestmark = pytest.mark.unit


class TestAPriorityLabel:
    """Coloured by level, so how urgent reads before which label has been read."""

    @pytest.mark.parametrize(
        ("level", "mark", "accent"),
        [
            (Priority.HIGH, "🔴", Accent.HIGH),
            (Priority.MEDIUM, "🟠", Accent.MEDIUM),
            (Priority.LOW, "🟢", Accent.LOW),
        ],
    )
    def test_each_level_has_its_own_colour(
        self, level: Priority, mark: str, accent: Accent
    ) -> None:
        move = LabelMove(name=level.value, added=True, priority=level)

        said = format_label_change(move)

        assert said.text == f"{mark} **Priority set:** `{level.value}`"
        assert said.accent == accent

    def test_whatever_the_repository_spells_it_is_coloured_by_what_it_means(self) -> None:
        """`urgent` is HIGH to the parser, so it is red here, and the label is still named as
        the repository writes it rather than translated into ours."""
        move = LabelMove(name="urgent", added=True, priority=Priority.HIGH)

        said = format_label_change(move)

        assert said.text == "🔴 **Priority set:** `urgent`"
        assert said.accent == Accent.HIGH

    def test_one_coming_off_has_no_colour_to_carry(self) -> None:
        """What a cleared priority leaves behind is not a level, so it is not given one. Grey
        rather than no bar at all: the card is still a card, it just has nothing urgent to say.
        """
        move = LabelMove(name="urgent", added=False, priority=Priority.HIGH)

        said = format_label_change(move)

        assert said.text == "⚪ **Priority cleared:** `urgent`"
        assert said.accent == Accent.NEUTRAL


class TestAStatusLabel:
    def test_one_going_on_is_a_status_set(self) -> None:
        move = LabelMove(name="IN_REVIEW", added=True, status=Status.IN_REVIEW)

        assert format_label_change(move).text == "📋 **Status set:** `IN_REVIEW`"

    def test_one_coming_off_is_a_status_cleared(self) -> None:
        move = LabelMove(name="BACKLOG", added=False, status=Status.BACKLOG)

        assert format_label_change(move).text == "📋 **Status cleared:** `BACKLOG`"


class TestAnOrdinaryLabel:
    def test_one_going_on_says_added(self) -> None:
        said = format_label_change(LabelMove(name="bug", added=True))

        assert said.text == "🏷️ Tag `bug` added."
        assert said.accent == Accent.NEUTRAL

    def test_one_coming_off_says_removed(self) -> None:
        assert format_label_change(LabelMove(name="bug", added=False)).text == (
            "🏷️ Tag `bug` removed."
        )


class TestEveryLineIsOneCard:
    """One block, so the card is a bar and a sentence and nothing else.

    Asserted once here rather than on each line above. What it stops is a line quietly growing a
    second block, which draws a rule across a card carrying a single sentence.
    """

    @pytest.mark.parametrize(
        "move",
        [
            LabelMove(name="urgent", added=True, priority=Priority.HIGH),
            LabelMove(name="urgent", added=False, priority=Priority.HIGH),
            LabelMove(name="DONE", added=True, status=Status.DONE),
            LabelMove(name="bug", added=True),
        ],
    )
    def test_it_is_a_heading_on_its_own(self, move: LabelMove) -> None:
        said = format_label_change(move)

        assert [block.kind for block in said.blocks] == [BlockKind.HEADING]
        assert not said.is_plain, "a tag line with no bar is the silent edit this file exists for"


class TestALabelNameNobodyHereChose:
    """A label is named by anybody with triage rights on the repository, so every group treats
    it as untrusted. This was a real hole in the metadata block once and is worth pinning on
    each of the three paths rather than on the one that happened to be written first."""

    def test_a_mention_cannot_ping_from_a_priority_line(self) -> None:
        move = LabelMove(name="<@1234>", added=True, priority=Priority.HIGH)

        assert "<@1234>" not in format_label_change(move).text

    def test_a_mention_cannot_ping_from_a_status_line(self) -> None:
        move = LabelMove(name="<@1234>", added=True, status=Status.DONE)

        assert "<@1234>" not in format_label_change(move).text

    def test_backticks_cannot_break_out_of_an_ordinary_tag_line(self) -> None:
        line = format_label_change(LabelMove(name="a`b", added=True))

        assert line.text == "🏷️ Tag ``a`b`` added."
