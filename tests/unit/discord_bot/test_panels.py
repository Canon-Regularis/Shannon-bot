"""What a panel is, before anything decides how it looks. Issues #116 and #113.

A panel is the value every renderer answers with now. It has no discord.py behind it, which is the
whole point: the wording is the most-tested thing in this project and none of it should need a UI
library to check.
"""

from __future__ import annotations

from typing import Any

import pytest

from shannon.discord_bot.panels import (
    PANEL_BUDGET,
    Accent,
    Block,
    BlockKind,
    Panel,
    PanelImage,
    PanelLink,
)
from shannon.discord_bot.safe_text import TRUNCATED

pytestmark = pytest.mark.unit

K = BlockKind


def panel(*blocks: tuple[BlockKind, str], **extra: Any) -> Panel:
    return Panel(blocks=tuple(Block(kind, text) for kind, text in blocks), **extra)


class TestWhatItReads:
    def test_the_blocks_in_order(self) -> None:
        said = panel((K.HEADING, "## a"), (K.FIELDS, "**b:** c"), (K.FOOTNOTE, "-# d"))

        assert said.text == "## a\n**b:** c\n-# d"

    def test_an_empty_panel_says_nothing(self) -> None:
        assert Panel().text == ""

    def test_length_counts_the_blocks_and_not_the_joins(self) -> None:
        """What Discord counts. The newlines `text` joins with are this project's, not the wire's:
        each block is its own component on the other side."""
        said = panel((K.HEADING, "abc"), (K.BODY, "de"))

        assert said.length() == 5
        assert len(said.text) == 6


class TestWhetherItIsACard:
    def test_plain_text_is_plain(self) -> None:
        assert Panel.of_text("hello").is_plain is True

    def test_empty_text_makes_no_block_at_all(self) -> None:
        """Several renderers answer with nothing to say, and a block holding an empty string would
        be a component carrying no words."""
        assert Panel.of_text("").blocks == ()

    @pytest.mark.parametrize(
        "extra",
        [
            {"accent": Accent.OPEN},
            {"thumbnail_url": "https://example.invalid/a.png"},
            {"link": PanelLink(label="Open", url="https://example.invalid")},
            {"images": (PanelImage(url="https://example.invalid/a.png"),)},
        ],
    )
    def test_anything_structural_stops_it_being_plain(self, extra: dict[str, Any]) -> None:
        assert panel((K.BODY, "hello"), **extra).is_plain is False

    def test_several_blocks_alone_do_not_make_a_card(self) -> None:
        """A different question from "does it have one block". A panel with two blocks and nothing
        structural to say is still an ordinary message."""
        assert panel((K.HEADING, "a"), (K.BODY, "b")).is_plain is True


class TestCuttingItToFit:
    def test_a_panel_inside_the_budget_is_returned_as_it_was(self) -> None:
        said = panel((K.BODY, "short"))

        assert said.trimmed() is said

    def test_whole_blocks_go_from_the_end(self) -> None:
        """The footnote before the description, and the description before the fields, so what a
        reader scans for is the last thing to give way. That was the block's rule already; this
        says it structurally rather than by arithmetic about lengths."""
        said = panel(
            (K.FIELDS, "f" * (PANEL_BUDGET - 10)),
            (K.BODY, "b" * 100),
            (K.FOOTNOTE, "-# gone"),
        )

        kept = said.trimmed()

        assert [block.kind for block in kept.blocks] == [K.FIELDS]

    def test_the_survivor_is_trimmed_on_a_line_boundary(self) -> None:
        said = panel((K.BODY, "\n".join("x" * 100 for _ in range(100))))

        kept = said.trimmed()

        assert kept.length() <= PANEL_BUDGET
        assert kept.blocks[0].text.endswith(TRUNCATED)

    def test_one_block_is_never_dropped_to_nothing(self) -> None:
        """A panel trimmed to no blocks at all is a message with nothing in it, which Discord
        refuses and which says less than a cut one."""
        said = panel((K.BODY, "y" * (PANEL_BUDGET * 2)))

        kept = said.trimmed()

        assert len(kept.blocks) == 1
        assert kept.length() <= PANEL_BUDGET

    def test_what_is_around_the_blocks_survives_the_cut(self) -> None:
        said = panel(
            (K.BODY, "z" * (PANEL_BUDGET + 10)),
            accent=Accent.MERGED,
            thumbnail_url="https://example.invalid/a.png",
        )

        kept = said.trimmed()

        assert kept.accent is Accent.MERGED
        assert kept.thumbnail_url == "https://example.invalid/a.png"


class TestTheColours:
    def test_the_states_a_reader_already_knows(self) -> None:
        """GitHub's own, so nobody has to learn a second vocabulary."""
        assert (Accent.OPEN, Accent.MERGED, Accent.CLOSED) == (0x3FB950, 0xA371F7, 0xF85149)

    def test_meanings_that_say_the_same_thing_share_a_colour(self) -> None:
        """A passing build and an open pull request tell a reader the same thing.

        Asserted on the values rather than on identity. They ARE the same member at runtime,
        because an enum folds two names onto one value into an alias, but a type checker models
        them as distinct literals and reads the identity check as one that can never hold.
        """
        assert Accent.PASSED.value == Accent.OPEN.value
        assert Accent.FAILED.value == Accent.CLOSED.value
        assert Accent.DRAFT.value == Accent.NEUTRAL.value


class TestThePictures:
    """Issue #126. They are structure rather than words, and the budget is about words."""

    def test_a_gallery_costs_nothing_against_the_budget(self) -> None:
        """Discord measures a view by its text displays, and a gallery is not one. So four
        pictures cost the same as none, which is why `length()` does not mention them."""
        bare = panel((K.BODY, "hello"))
        shown = panel((K.BODY, "hello"), images=tuple(PanelImage(url=f"u{n}") for n in range(4)))

        assert shown.length() == bare.length()

    def test_a_card_whose_only_structure_is_a_gallery_is_not_plain(self) -> None:
        """Otherwise it would go out as the text it came from and the pictures would not appear."""
        assert panel((K.BODY, "hello"), images=(PanelImage(url="u"),)).is_plain is False

    def test_the_pictures_survive_a_cut(self) -> None:
        """They cost nothing, so dropping them would buy nothing."""
        long = panel((K.BODY, "w" * (PANEL_BUDGET + 50)), images=(PanelImage(url="u"),))

        assert long.trimmed().images == (PanelImage(url="u"),)
