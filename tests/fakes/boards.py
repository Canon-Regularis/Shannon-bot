"""A project board that answers with whatever it was set to hold, and cards to put on it.

Shared rather than private to one test file, because two things mirror a draft card now: the poller
on a timer, and `/refresh tickets` when somebody asks. These lived inside
`tests/integration/test_project_polling.py`, a file long enough that a second copy of `card()` was
the likelier outcome than an import.

This file is on neither checker's ratchet in `pyproject.toml`, so it is held to mypy strict and
pyright strict. That is deliberate: the rule there is that a file never goes back on the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from shannon.domain.enums import ObjectType
from shannon.github.projects import BoardItem

# The board number every one of these cards claims to be on. A card carries no number of its own,
# so this is what a ticket's row records instead.
PROJECT = 12


def card(
    item_id: int = 900,
    title: str = "Write the poller",
    column: str | None = "In Progress",
    at: str = "2026-08-20T10:00:00Z",
) -> BoardItem:
    """A DRAFT card: `BoardItem.kind` defaults to TICKET and `content_id` to None, which is what
    `is_draft` reads. There is nothing on GitHub behind it."""
    return BoardItem(
        item_id=item_id,
        title=title,
        column=column,
        html_url=f"https://github.com/users/Canon-Regularis/projects/{PROJECT}",
        updated_at=datetime.fromisoformat(at.replace("Z", "+00:00")),
    )


def wraps(kind: ObjectType, content_id: int, column: str = "Done", item_id: int = 700) -> BoardItem:
    """A card that wraps something already mirrored from its own webhooks.

    The counterpart to `card`, and the negative every ticket test needs: a scope that mirrors
    drafts must leave these alone, because they already have a thread of their own.
    """
    return BoardItem(
        item_id=item_id,
        kind=kind,
        title="Add the webhook endpoint",
        column=column,
        html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        content_id=content_id,
        updated_at=datetime.fromisoformat("2026-08-20T10:00:00+00:00"),
    )


class FakeBoard:
    """A project board that answers with whatever it was last set to hold."""

    def __init__(self, *items: BoardItem) -> None:
        self.items = list(items)
        self.reads: list[tuple[str, int]] = []
        self.error: Exception | None = None

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        self.reads.append((owner, project_number))
        if self.error is not None:
            raise self.error
        return list(self.items)
