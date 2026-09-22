"""Turning a panel into something Discord will draw.

The only module that imports `discord.ui`; `formatting` decides the wording and answers with a
`Panel` carrying no UI library. A Hypothesis property asserts the law: the view's text displays
are exactly the panel's block texts, in the panel's order. A plain panel goes out as a string,
because any `LayoutView` sets the components flag and Discord never lets it come off a message.
"""

from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.panels import BlockKind, Panel, PanelImage, PanelLink

# Every component is generic in the view it belongs to, and a bare `ui.TextDisplay("x")` leaves
# that parameter unsolved, which both type checkers refuse.
type Part = ui.Item[ui.LayoutView]
Text = ui.TextDisplay[ui.LayoutView]
Rule = ui.Separator[ui.LayoutView]
Group = ui.Section[ui.LayoutView]
Box = ui.Container[ui.LayoutView]
Picture = ui.Thumbnail[ui.LayoutView]
Row = ui.ActionRow[ui.LayoutView]
Gallery = ui.MediaGallery[ui.LayoutView]
# Not generic and not exported from `discord.ui`: a gallery item is a value rather than an
# `Item`. That is also why a gallery costs one component however many pictures it holds.
Shown = discord.MediaGalleryItem

# Alt text for the thumbnail. Generic because this module does not know who the panel is about.
PICTURE_ALT = "Avatar"


def as_message(panel: Panel) -> tuple[str | None, ui.LayoutView | None]:
    """What to send: either text or a view, and never both.

    Discord refuses a message carrying components alongside content, so this returns the one that
    applies and `None` for the other.
    """
    cut = panel.trimmed()
    if panel.is_plain:
        return cut.text, None
    return None, as_view(cut)


def as_view(panel: Panel) -> ui.LayoutView:
    """The panel as components."""
    view = ui.LayoutView(timeout=None)
    for item in _framed(panel):
        view.add_item(item)
    return view


def _framed(panel: Panel) -> list[Part]:
    parts = _parts(panel)
    if panel.accent is None:
        return parts
    return [Box(*parts, accent_colour=discord.Colour(panel.accent.value))]


def _parts(panel: Panel) -> list[Part]:
    """Every block as a component, with the rules and the picture around them.

    The picture hangs off whatever the panel opens with, not off a heading: the block at the top
    of a thread has no heading, and its rows are one block so that they read as a table. A heading
    takes the subheading after it into one section, paired by position rather than by kind,
    because a panel may carry two headings and both must be drawn.
    """
    parts: list[Part] = []
    blocks = list(panel.blocks)
    picture = panel.thumbnail_url
    index = 0
    while index < len(blocks):
        block = blocks[index]
        index += 1
        under = ""
        if block.kind is BlockKind.HEADING and index < len(blocks):
            following = blocks[index]
            if following.kind is BlockKind.SUBHEADING:
                under = following.text
                index += 1

        parts.extend(_before(block.kind, started=bool(parts)))
        parts.extend(_opening(block.text, under, picture))
        picture = None

    if panel.images:
        parts.append(_gallery(panel.images))
    if panel.link is not None:
        parts.append(Row(_button(panel.link)))
    return parts


def _gallery(images: tuple[PanelImage, ...]) -> Part:
    """The pictures the description was written around, as one gallery component.

    No guard on the count: Discord takes ten and `rich_text.IMAGES_SHOWN` is four, so a check
    here would be a branch nothing can take, which a coverage floor of a hundred per cent
    forbids.
    """
    return Gallery(*(Shown(image.url, description=image.alt) for image in images))


def _before(kind: BlockKind, *, started: bool) -> list[Part]:
    """The rule that goes above a block, where one does."""
    if not started:
        return []
    if kind is BlockKind.BODY:
        return [Rule(spacing=discord.SeparatorSpacing.large)]
    if kind in (BlockKind.FIELDS, BlockKind.FOOTNOTE):
        return [Rule(spacing=discord.SeparatorSpacing.small)]
    return []


def _opening(said: str, under: str, picture: str | None) -> list[Part]:
    """One block, the small line under it where there is one, and the picture beside them.

    A list rather than one part: `Section.accessory` is required and has no default, so with no
    avatar to show there is no section to make, only two bare text displays side by side.
    """
    lines = [Text(said)] if not under else [Text(said), Text(under)]
    if picture is None:
        return list(lines)
    return [Group(*lines, accessory=Picture(picture, description=PICTURE_ALT))]


def _button(link: PanelLink) -> ui.Button[ui.LayoutView]:
    """A link button, which is the one kind that needs nothing behind it.

    A button carrying a URL has no custom id, so discord.py treats the view as not dispatchable,
    never stores it, and starts no timeout task to leak.
    """
    return ui.Button(style=discord.ButtonStyle.link, url=link.url, label=link.label)


def words(view: ui.LayoutView) -> list[str]:
    """Every text display in the view, in order. What the law is asserted against."""
    return [item.content for item in view.walk_children() if isinstance(item, ui.TextDisplay)]


def parts_in(view: ui.LayoutView) -> int:
    """How many components the view holds, counting nested ones the way Discord does."""
    return len(list(view.walk_children()))
