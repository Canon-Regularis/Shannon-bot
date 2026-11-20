"""Reading a built view back, for the tests that hold `layout` to its law. Issue #269.

Here rather than in `layout`, which is where they lived: the bot never reads a view it built, so
in production they were public names nothing called, covered only because tests called them.
"""

from __future__ import annotations

from discord import ui


def words(view: ui.LayoutView) -> list[str]:
    """Every text display in the view, in order. What the law is asserted against."""
    return [item.content for item in view.walk_children() if isinstance(item, ui.TextDisplay)]


def parts_in(view: ui.LayoutView) -> int:
    """How many components the view holds, counting nested ones the way Discord does."""
    return len(list(view.walk_children()))
