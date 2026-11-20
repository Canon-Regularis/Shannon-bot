"""Turning a panel into something Discord will draw.

The only module that builds `discord.ui` components (`responses` and `threads` import `ui` only
to type the `LayoutView` they send); `formatting` decides the wording and answers with a `Panel`
carrying no UI library. A Hypothesis property in `test_layout` asserts the law: the view's text
displays are exactly the panel's block texts, in the panel's order. A plain panel goes out as a
string, because any `LayoutView` sets the components flag and Discord never lets it come off a
message.
"""

from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.panels import BlockKind, Panel, PanelImage, PanelLink
from shannon.discord_bot.safe_text import fit_characters

# Every component is generic in the view it belongs to, and a bare `ui.TextDisplay("x")` leaves
# that parameter unsolved, which both type checkers refuse.
type _Part = ui.Item[ui.LayoutView]
_Text = ui.TextDisplay[ui.LayoutView]
_Rule = ui.Separator[ui.LayoutView]
_Group = ui.Section[ui.LayoutView]
_Box = ui.Container[ui.LayoutView]
_Picture = ui.Thumbnail[ui.LayoutView]
_Row = ui.ActionRow[ui.LayoutView]
_Gallery = ui.MediaGallery[ui.LayoutView]
# Not generic and not exported from `discord.ui`: a gallery item is a value rather than an
# `Item`. That is also why a gallery costs one component however many pictures it holds.
_Shown = discord.MediaGalleryItem

# Alt text for the thumbnail. Generic because this module does not know who the panel is about.
_PICTURE_ALT = "Avatar"


def as_message(panel: Panel) -> tuple[str | None, ui.LayoutView | None]:
    """What to send: either text or a view, and never both.

    Discord refuses a message carrying components alongside content, so this returns the one that
    applies and `None` for the other.
    """
    cut = panel.trimmed()
    if panel.is_plain:
        # Cut again, and here rather than in any one sender, since #269: `trimmed` measures against
        # the budget a view gets and content gets half of it. At a character rather than a line,
        # because the replies were always cut that way and this keeps every one of them the same.
        return fit_characters(cut.text), None
    return None, as_view(cut)


def as_view(panel: Panel) -> ui.LayoutView:
    """The panel as components."""
    view = ui.LayoutView(timeout=None)
    for item in _framed(panel):
        view.add_item(item)
    return view


def _framed(panel: Panel) -> list[_Part]:
    parts = _parts(panel)
    if panel.accent is None:
        return parts
    return [_Box(*parts, accent_colour=discord.Colour(panel.accent.value))]


def _parts(panel: Panel) -> list[_Part]:
    """Every block as a component, with the rules and the picture around them.

    The picture hangs off whatever the panel opens with, not off a heading: the block at the top
    of a thread has no heading, and its rows are one block so that they read as a table. A heading
    takes the subheading after it into one section, paired by position rather than by kind,
    because a panel may carry two headings and both must be drawn.
    """
    parts: list[_Part] = []
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
        parts.append(_Row(_button(panel.link)))
    return parts


def _gallery(images: tuple[PanelImage, ...]) -> _Part:
    """The pictures the description was written around, as one gallery component.

    No guard on the count: Discord takes ten and `rich_text.IMAGES_SHOWN` is four, so a check
    here would be a branch nothing can take, which a coverage floor of a hundred per cent
    forbids.
    """
    return _Gallery(*(_Shown(image.url, description=image.alt) for image in images))


def _before(kind: BlockKind, *, started: bool) -> list[_Part]:
    """The rule that goes above a block, where one does."""
    if not started:
        return []
    if kind is BlockKind.BODY:
        return [_Rule(spacing=discord.SeparatorSpacing.large)]
    if kind in (BlockKind.FIELDS, BlockKind.FOOTNOTE):
        return [_Rule(spacing=discord.SeparatorSpacing.small)]
    return []


def _opening(said: str, under: str, picture: str | None) -> list[_Part]:
    """One block, the small line under it where there is one, and the picture beside them.

    A list rather than one part: `Section.accessory` is required and has no default, so with no
    avatar to show there is no section to make, only two bare text displays side by side.
    """
    lines = [_Text(said)] if not under else [_Text(said), _Text(under)]
    if picture is None:
        return list(lines)
    return [_Group(*lines, accessory=_Picture(picture, description=_PICTURE_ALT))]


def _button(link: PanelLink) -> ui.Button[ui.LayoutView]:
    """A link button, which is the one kind that needs nothing behind it.

    A button carrying a URL has no custom id, so discord.py treats the view as not dispatchable,
    never stores it, and starts no timeout task to leak.
    """
    return ui.Button(style=discord.ButtonStyle.link, url=link.url, label=link.label)
