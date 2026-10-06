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
from shannon.domain.models import Actor, Label
from shannon.github.projects import BoardItem

# The board number every one of these cards claims to be on. A card carries no number of its own,
# so this is what a ticket's row records instead.
PROJECT = 12


def card(
    item_id: int = 900,
    title: str = "Write the poller",
    column: str | None = "In Progress",
    at: str = "2026-08-20T10:00:00Z",
    *,
    creator: Actor | None = None,
    assignees: tuple[Actor, ...] = (),
    labels: tuple[Label, ...] = (),
    priority_name: str | None = None,
    story_point: str | None = None,
    iteration: str | None = None,
    area: str | None = None,
    body: str = "",
    archived: bool = False,
) -> BoardItem:
    """A DRAFT card: `BoardItem.kind` defaults to TICKET and `content_id` to None, which is what
    `is_draft` reads. There is nothing on GitHub behind it.

    Everything after `at` is the board metadata issue #182 added, and every one of them defaults to
    ABSENT rather than set. That is not laziness: a board that has not got the field and a card
    nobody filled in reach the same place, and most tests want a card that carries nothing so the
    thing they are about is the only thing in the block. A test about the metadata names what it
    needs; see `filled` below for one that carries the lot.

    `archived` is a card in the board's archive, issue #198: read and marked, never mirrored.
    """
    return BoardItem(
        item_id=item_id,
        title=title,
        column=column,
        html_url=f"https://github.com/users/Canon-Regularis/projects/{PROJECT}",
        updated_at=datetime.fromisoformat(at.replace("Z", "+00:00")),
        creator=creator,
        assignees=assignees,
        labels=labels,
        priority_name=priority_name,
        story_point=story_point,
        iteration=iteration,
        area=area,
        body=body,
        archived=archived,
    )


def filled() -> BoardItem:
    """A card with every board field set, as a real board answered for a real draft.

    The values are shapes observed on the wire rather than invented ones: a single-select priority
    of `HIGH`, a story point of `05` zero-padded rather than numbered, an iteration named for
    itself, and an area.

    No overrides parameter, deliberately. A `**kwargs` passthrough would need a `type: ignore` to
    get past the checkers this file is deliberately held to, and `dataclasses.replace` says the
    same thing in the test that wants it - typed, and next to the assertion it is for.
    """
    return card(
        creator=Actor("octocat", github_user_id=1, avatar_url="https://avatars.example/u/1"),
        assignees=(Actor("hubot"),),
        labels=(Label("high priority"),),
        priority_name="HIGH",
        story_point="05",
        iteration="Iteration 1",
        area="General",
        body="what the card asks for",
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

    def __init__(self, *items: BoardItem, cheap: bool = True) -> None:
        self.items = list(items)
        self.reads: list[tuple[str, int]] = []
        self.error: Exception | None = None
        # Whether the poller is told this board can be re-read for a conditional request. True by
        # default, which is what a board small enough to fit in one page answers and therefore
        # what nearly every test wants. `cheap=False` is the board that has outgrown a page, and
        # the only thing that changes is how long the poller waits before reading it again.
        self.cheap = cheap
        # Issue #198. Cards GitHub still has that the listing leaves out - an archived card, where
        # the listing leaves the archive out, or one a capped read never reached. `read_card`
        # answers for them and the listing never does.
        self.unlisted: list[BoardItem] = []
        # Every card asked about on its own, as (owner, number, card id), and what to raise
        # instead of answering.
        self.card_reads: list[tuple[str, int, int]] = []
        self.card_error: Exception | None = None

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]:
        self.reads.append((owner, project_number))
        if self.error is not None:
            raise self.error
        return list(self.items)

    async def read_card(self, owner: str, project_number: int, card_id: int) -> BoardItem | None:
        """One card on its own: the card the board has under that id, listed or not, or None -
        which is GitHub answering 404 for a card it does not have."""
        self.card_reads.append((owner, project_number, card_id))
        if self.card_error is not None:
            raise self.card_error
        return next((one for one in (*self.items, *self.unlisted) if one.item_id == card_id), None)

    def can_recheck_cheaply(self, owner: str, project_number: int) -> bool:
        return self.cheap
