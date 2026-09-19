"""Turning a panel into components, and the one law that makes the rest of the change safe.

Issue #116. The property at the bottom of `TestTheLaw` is the load-bearing test in this whole
piece of work. Everything else rests on `Panel.text` being a projection of what the view says
rather than a second renderer that could drift from it: the thread fake records `panel.text`, which
is what lets several hundred existing assertions about message text keep meaning something while
production sends components. If the law does not hold, those assertions are fiction.
"""

from __future__ import annotations

from typing import Any

import discord
import pytest
from discord import ui
from hypothesis import given
from hypothesis import strategies as st

from shannon.discord_bot.layout import as_message, as_view, parts_in, words
from shannon.discord_bot.panels import (
    PANEL_BUDGET,
    Accent,
    Block,
    BlockKind,
    Panel,
    PanelImage,
    PanelLink,
)

pytestmark = pytest.mark.unit

K = BlockKind
AVATAR = "https://avatars.githubusercontent.com/u/1?v=4"
SHOT = "https://user-images.githubusercontent.com/1/a.png"


def panel(*blocks: tuple[BlockKind, str], **extra: Any) -> Panel:
    return Panel(blocks=tuple(Block(kind, text) for kind, text in blocks), **extra)


BLOCK = panel(
    (K.HEADING, "## #7 Add the webhook endpoint"),
    (K.SUBHEADING, "-# acme/widget"),
    (K.FIELDS, "**Type:** PR"),
    (K.BODY, "**Description**\nWhy"),
    (K.FOOTNOTE, "-# just now"),
    accent=Accent.OPEN,
)


class TestTheLaw:
    """Grouping, rules, the bar, the picture and the button add structure and never words."""

    def test_the_view_says_the_panels_words_in_the_panels_order(self) -> None:
        assert words(as_view(BLOCK)) == [block.text for block in BLOCK.blocks]

    def test_a_picture_changes_the_shape_and_not_the_words(self) -> None:
        with_one = as_view(Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, thumbnail_url=AVATAR))

        assert words(with_one) == words(as_view(BLOCK))

    def test_a_button_adds_no_words(self) -> None:
        linked = Panel(
            blocks=BLOCK.blocks,
            accent=Accent.OPEN,
            link=PanelLink(label="Open on GitHub", url="https://example.invalid"),
        )

        assert words(as_view(linked)) == words(as_view(BLOCK))

    @given(
        st.lists(
            st.tuples(st.sampled_from(list(BlockKind)), st.text(min_size=1, max_size=120)),
            min_size=1,
            max_size=5,
        ),
        st.one_of(st.none(), st.sampled_from(list(Accent))),
        st.one_of(st.none(), st.just(AVATAR)),
        st.lists(st.just(PanelImage(url=SHOT)), max_size=4).map(tuple),
    )
    def test_it_holds_for_any_panel(
        self,
        blocks: list[tuple[BlockKind, str]],
        accent: Accent | None,
        thumbnail: str | None,
        images: tuple[PanelImage, ...],
    ) -> None:
        """The one that carries the design. Without it, recording `panel.text` in the fake is a
        claim nobody is checking.

        Generated WITH pictures since issue #126, which is the only honest way to say the law
        still holds now that a card can carry a part that draws rather than speaks.
        """
        said = panel(*blocks, accent=accent, thumbnail_url=thumbnail, images=images)

        view = as_view(said)

        assert words(view) == [block.text for block in said.blocks]
        assert view.content_length() == said.length()


class TestWhatGoesOnTheWire:
    def test_a_plain_panel_is_sent_as_the_text_it_always_was(self) -> None:
        """What lets this change reach every send site without moving a byte. It is NOT sent as a
        view with no container: any layout view carries the components flag, and Discord will not
        let that flag come off a message once it has seen it."""
        text, view = as_message(Panel.of_text("-# a quiet signpost"))

        assert text == "-# a quiet signpost"
        assert view is None

    def test_a_card_is_sent_as_a_view_and_never_as_both(self) -> None:
        text, view = as_message(BLOCK)

        assert text is None
        assert view is not None
        assert view.has_components_v2() is True

    def test_an_oversized_panel_is_cut_before_it_is_built(self) -> None:
        huge = panel((K.BODY, "x" * (PANEL_BUDGET * 2)), accent=Accent.OPEN)

        _, view = as_message(huge)

        assert view is not None
        assert view.content_length() <= PANEL_BUDGET


class TestTheShape:
    def test_an_accent_wraps_everything_in_one_container(self) -> None:
        view = as_view(BLOCK)

        top = view.to_components()
        assert len(top) == 1
        assert top[0]["accent_color"] == Accent.OPEN.value

    def test_no_accent_means_no_container(self) -> None:
        """A panel with a picture but no colour is still structural, and still not a card."""
        view = as_view(Panel(blocks=BLOCK.blocks, thumbnail_url=AVATAR))

        assert not any(isinstance(item, ui.Container) for item in view.walk_children())

    def test_a_picture_makes_the_heading_a_section(self) -> None:
        view = as_view(Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, thumbnail_url=AVATAR))

        sections = [item for item in view.walk_children() if isinstance(item, ui.Section)]
        assert len(sections) == 1
        assert isinstance(sections[0].accessory, ui.Thumbnail)

    def test_a_panel_that_opens_with_fields_carries_its_picture_too(self) -> None:
        """The item block's own shape. Its eleven rows are one block so that they read as a
        table, so it has no heading at all, and a picture attached to headings specifically
        would have dropped the author's face from the one message in the project that has one.
        """
        block = panel(
            (K.FIELDS, "**Type:** PR"),
            (K.BODY, "**Description:**\nWhy"),
            accent=Accent.OPEN,
            thumbnail_url=AVATAR,
        )

        view = as_view(block)

        sections = [item for item in view.walk_children() if isinstance(item, ui.Section)]
        assert len(sections) == 1
        assert isinstance(sections[0].accessory, ui.Thumbnail)
        assert words(view) == [part.text for part in block.blocks]

    def test_only_the_first_block_carries_it(self) -> None:
        """One picture per card. Discord hangs a thumbnail off a section, and a section per block
        would put the same avatar down the whole message.
        """
        view = as_view(Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, thumbnail_url=AVATAR))

        pictures = [item for item in view.walk_children() if isinstance(item, ui.Thumbnail)]
        assert len(pictures) == 1

    def test_without_one_there_is_no_section_at_all(self) -> None:
        """Forced rather than chosen: a section's accessory is required and has no default, so
        there is no such thing as a section with nothing hanging off it."""
        view = as_view(BLOCK)

        assert not any(isinstance(item, ui.Section) for item in view.walk_children())

    def test_a_rule_separates_the_body_from_what_is_above_it(self) -> None:
        """Issue #113. This is the separation the blockquote markers were doing badly."""
        view = as_view(BLOCK)

        rules = [item for item in view.walk_children() if isinstance(item, ui.Separator)]
        assert any(rule.spacing is discord.SeparatorSpacing.large for rule in rules)

    def test_nothing_is_ruled_off_from_the_top_of_the_panel(self) -> None:
        view = as_view(panel((K.BODY, "alone"), accent=Accent.OPEN))

        assert not any(isinstance(item, ui.Separator) for item in view.walk_children())

    def test_a_link_is_one_button_that_nothing_has_to_answer(self) -> None:
        linked = Panel(
            blocks=BLOCK.blocks,
            accent=Accent.OPEN,
            link=PanelLink(label="Open on GitHub", url="https://example.invalid/x"),
        )

        view = as_view(linked)

        buttons = [item for item in view.walk_children() if isinstance(item, ui.Button)]
        assert len(buttons) == 1
        assert buttons[0].url == "https://example.invalid/x"
        assert view.is_dispatchable() is False, "a stored view would need a handler and leak"


def test_the_widest_panel_this_project_builds_is_far_under_the_ceiling() -> None:
    """Discord allows forty components. One text display per block rather than one per line is
    what keeps that unreachable, and it is proved here rather than guarded at runtime: a branch
    nothing can take and a coverage floor of a hundred per cent do not mix.
    """
    widest = Panel(
        blocks=tuple(Block(kind, "x") for kind in BlockKind),
        accent=Accent.OPEN,
        thumbnail_url=AVATAR,
        link=PanelLink(label="Open on GitHub", url="https://example.invalid"),
        images=tuple(PanelImage(url=SHOT) for _ in range(4)),
    )

    assert parts_in(as_view(widest)) < 20


class TestThePictures:
    """Issue #126. Discord renders no inline image anywhere, so one has to be a component."""

    def test_no_pictures_means_no_gallery(self) -> None:
        assert not any(isinstance(item, ui.MediaGallery) for item in as_view(BLOCK).walk_children())

    def test_a_gallery_is_one_component_however_many_pictures(self) -> None:
        """Its items are values rather than items, so nothing walks into it. That is what keeps
        four pictures costing what one does against the ceiling of forty."""
        one = Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, images=(PanelImage(url=SHOT),))
        four = Panel(
            blocks=BLOCK.blocks,
            accent=Accent.OPEN,
            images=tuple(PanelImage(url=f"{SHOT}?{n}") for n in range(4)),
        )

        assert parts_in(as_view(four)) == parts_in(as_view(one))
        assert parts_in(as_view(four)) == parts_in(as_view(BLOCK)) + 1

    def test_it_adds_no_words_and_no_length(self) -> None:
        """The law, said about the part that draws rather than speaks."""
        shown = Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, images=(PanelImage(url=SHOT),))

        assert words(as_view(shown)) == words(as_view(BLOCK))
        assert as_view(shown).content_length() == as_view(BLOCK).content_length()

    def test_it_sits_under_the_blocks_and_above_the_button(self) -> None:
        """They belong to the description they came out of, and the button is always last."""
        shown = Panel(
            blocks=BLOCK.blocks,
            accent=Accent.OPEN,
            images=(PanelImage(url=SHOT),),
            link=PanelLink(label="Open on GitHub", url="https://example.invalid"),
        )

        drawn = [type(item).__name__ for item in as_view(shown).walk_children()]
        assert drawn.index("MediaGallery") > max(
            index for index, name in enumerate(drawn) if name == "TextDisplay"
        )
        assert drawn.index("MediaGallery") < drawn.index("ActionRow")

    def test_the_words_of_a_picture_reach_the_item(self) -> None:
        shown = Panel(
            blocks=BLOCK.blocks,
            accent=Accent.OPEN,
            images=(PanelImage(url=SHOT, alt="the stack trace"),),
        )

        gallery = next(
            item for item in as_view(shown).walk_children() if isinstance(item, ui.MediaGallery)
        )
        assert gallery.items[0].description == "the stack trace"

    def test_a_picture_with_no_words_carries_none(self) -> None:
        shown = Panel(blocks=BLOCK.blocks, accent=Accent.OPEN, images=(PanelImage(url=SHOT),))

        gallery = next(
            item for item in as_view(shown).walk_children() if isinstance(item, ui.MediaGallery)
        )
        assert gallery.items[0].description is None
