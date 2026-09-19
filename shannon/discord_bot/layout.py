"""Turning a panel into something Discord will draw. Issue #116.

The only module in this project that imports `discord.ui`, deliberately. Everything that decides
what a message SAYS lives in `formatting` and answers with a `Panel`, which is a value object with
no UI library behind it; this is the one place that knows what a container or a separator is. That
keeps the wording testable without discord.py and keeps the component library out of the two
modules neither type checker looks at.

**The law this module exists to keep:**

    The text displays in the view it builds are exactly the panel's block texts, in the panel's
    order. Grouping, rules, the accent bar, the picture and the button add structure and never
    words.

That is what makes `Panel.text` a projection rather than a second renderer, and `Panel.text` is
what lets the thread fake keep recording a readable string while production sends components. A
Hypothesis property asserts the law over generated panels, and without it none of that is safe.

**A plain panel gets no view at all.** Not a view without a container: any `LayoutView` carries the
components flag, and that flag can never be taken off a message once Discord has seen it. So a
message that has nothing structural to say is sent as the string it always was, which is also what
lets the change reach every send site without changing a byte on the wire.
"""

from __future__ import annotations

import discord
from discord import ui

from shannon.discord_bot.panels import BlockKind, Panel, PanelLink

# Every component is generic in the view it belongs to, and a bare `ui.TextDisplay("x")` leaves
# that parameter unsolved, which both type checkers refuse. Pinned once here so that nothing below
# needs an annotation, and so the pinning is explained in one place rather than at every use.
type Part = ui.Item[ui.LayoutView]
Text = ui.TextDisplay[ui.LayoutView]
Rule = ui.Separator[ui.LayoutView]
Group = ui.Section[ui.LayoutView]
Box = ui.Container[ui.LayoutView]
Picture = ui.Thumbnail[ui.LayoutView]
Row = ui.ActionRow[ui.LayoutView]

# What a thumbnail is described as for somebody using a screen reader. Generic because the picture
# is whoever the panel is about and this module does not know who that is.
PICTURE_ALT = "Avatar"


def as_message(panel: Panel) -> tuple[str | None, ui.LayoutView | None]:
    """What to send: either text or a view, and never both.

    Discord refuses a message carrying components alongside content, so this returns the one that
    applies and `None` for the other. Every send site takes the pair and passes it straight
    through, which is why there is no branch on panel shape anywhere in the gateway.
    """
    cut = panel.trimmed()
    if panel.is_plain:
        return cut.text, None
    return None, as_view(cut)


def as_view(panel: Panel) -> ui.LayoutView:
    """The panel as components.

    Built even for a panel with no accent, because `as_message` has already decided a plain one
    goes out as text. Anything reaching here has something structural to say.
    """
    view = ui.LayoutView(timeout=None)
    for item in _framed(panel):
        view.add_item(item)
    return view


def _framed(panel: Panel) -> list[Part]:
    """The panel's parts, inside a container when it has a colour to carry."""
    parts = _parts(panel)
    if panel.accent is None:
        return parts
    return [Box(*parts, accent_colour=discord.Colour(panel.accent.value))]


def _parts(panel: Panel) -> list[Part]:
    """Every block as a component, with the rules and the picture around them.

    The order of the blocks is the panel's and is never rearranged. What this decides is only what
    sits BETWEEN them: a rule before the body, because that separation is what the blockquote was
    doing badly and is the whole of issue #113, and a quieter one before the fields and footnote.

    **The picture hangs off whatever the panel OPENS with**, which is a heading on the lines in a
    thread and the field rows on the block at the top of one. Not off the heading specifically:
    the block has no heading, its eleven rows are one block so that they read as a table, and a
    rule against kind would have dropped the author's face from the one message that has one.

    A heading swallows the subheading that follows it, because a picture hangs off a section and a
    section is what the two of them become. Read off the blocks in hand rather than looked up by
    kind: a panel carrying two headings must draw both, and an earlier version that asked the
    panel for "the heading" drew the first one twice. The property test found that.
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

    if panel.link is not None:
        parts.append(Row(_button(panel.link)))
    return parts


def _before(kind: BlockKind, *, started: bool) -> list[Part]:
    """The rule that goes above a block, where one does.

    Nothing above the first thing in a panel, because a rule needs something to separate from.
    """
    if not started:
        return []
    if kind is BlockKind.BODY:
        return [Rule(spacing=discord.SeparatorSpacing.large)]
    if kind in (BlockKind.FIELDS, BlockKind.FOOTNOTE):
        return [Rule(spacing=discord.SeparatorSpacing.small)]
    return []


def _opening(said: str, under: str, picture: str | None) -> list[Part]:
    """One block, the small line under it where there is one, and the picture beside them.

    A LIST rather than one part, and that is forced rather than chosen. `Section.accessory` is
    required and has no default, so there is no such thing as a section without a picture: with no
    avatar to show, a block and the line under it are two bare text displays next to each other,
    and only with one do they become a section something can hang off.

    All four shapes say the same words in the same order, which is the law this module keeps. What
    differs is only what surrounds them.
    """
    lines = [Text(said)] if not under else [Text(said), Text(under)]
    if picture is None:
        return list(lines)
    return [Group(*lines, accessory=Picture(picture, description=PICTURE_ALT))]


def _button(link: PanelLink) -> ui.Button[ui.LayoutView]:
    """A link button, which is the one kind that needs nothing behind it.

    A button carrying a URL has no custom id, so the view reports itself as not dispatchable,
    discord.py never stores it, and there is no timeout task and nothing to leak. That is why the
    one interactive-looking thing in this project still needs no handler anywhere.
    """
    return ui.Button(style=discord.ButtonStyle.link, url=link.url, label=link.label)


def words(view: ui.LayoutView) -> list[str]:
    """Every text display in the view, in order. What the law is asserted against."""
    return [item.content for item in view.walk_children() if isinstance(item, ui.TextDisplay)]


def parts_in(view: ui.LayoutView) -> int:
    """How many components the view holds, counting nested ones the way Discord does."""
    return len(list(view.walk_children()))
