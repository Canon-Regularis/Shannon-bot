"""What a draft card on a project board is, for everything that mirrors one.

A draft card is the one tracked thing that no webhook ever mentions. An issue or a pull request
announces itself; a card exists only on the board, so the only way to learn about one is to go and
look. The poller looks on a timer and `/refresh tickets` looks when somebody asks, and both need
the same three answers: how to read a board, how to turn a card into a snapshot, and what to put
back when a mirror fails halfway.

A leaf module rather than the poller's own, and not because of an import cycle — `services/sync`
importing `services/projects` is acyclic today. It is because the backlog mirror has no business
importing from the poller: they are peers that happen to share a subject, and the shared part is
small, pure and testable on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import TITLE_WIDTH, URL_WIDTH
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.models import RepositorySnapshot, TicketSnapshot
from shannon.github.projects import BoardItem

logger = logging.getLogger(__name__)


class ReadsBoards(Protocol):
    """Listing what is on a project board, which is all either caller asks of GitHub."""

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]: ...

    def can_recheck_cheaply(self, owner: str, project_number: int) -> bool:
        """Whether another read of this board would cost a conditional request or the whole body.

        Here rather than on a protocol of the poller's own because it is a fact about the reader's
        last answer, and the poller is the only caller that acts on it: `/refresh` reads a board
        once because somebody asked it to, where the poller reads one over and over and has to
        care what each read costs.
        """
        ...


def snapshot_of(
    item: BoardItem, *, repository: RepositorySnapshot, project_number: int, action: str
) -> TicketSnapshot:
    """One card, as the thing the sync path mirrors.

    Takes the repository and the board number rather than the poller's own board type, so nothing
    here depends on who is calling. `action` is the caller's name for why it looked, which is the
    only thing the two have to disagree about.
    """
    return TicketSnapshot(
        repository=repository,
        # The CARD id. A draft has no GitHub object of its own, so this is its only identity, and
        # it is what every lookup of a ticket row keys on.
        github_object_id=item.item_id,
        # A card has no number of its own, so the board's is carried instead. It is what a
        # reader of the row has to go on to find where the thing came from.
        number=project_number,
        # Cut to what the row holds. A draft card's Title is a free text field with no cap
        # on GitHub's side, unlike an issue's, and one card too wide for the column ends the
        # whole poll rather than that one card.
        title=item.title[:TITLE_WIDTH],
        html_url=item.html_url[:URL_WIDTH],
        state="open",
        updated_at=item.updated_at,
        action=action,
        column=item.column,
        project_number=project_number,
        # Issue #182. All of it optional at the source: a board without the field, or a card with
        # nothing set in it, answers None or an empty tuple, and the block leaves that row out.
        #
        # The creator goes in `author`, which is where every other snapshot keeps the person a
        # thread is about - so the block's Author row and the avatar beside it start working for a
        # card without either of them learning that a ticket exists.
        author=item.creator,
        assignees=item.assignees,
        # A draft's own text. `BoardItem.body` is empty for anything else, and an issue's body
        # reached its thread from its own webhook long before any board was read.
        body=item.body,
        labels=item.labels,
        created_at=item.created_at,
        priority_name=item.priority_name,
        story_point=item.story_point,
        iteration=item.iteration,
        area=item.area,
    )


def once_each(items: Sequence[BoardItem]) -> list[BoardItem]:
    """The board's cards, with any the read handed back twice dropped.

    A board is read a page at a time by cursor, and a cursor is not a snapshot: GitHub says
    outright that a list edited while it is being paged through can hand the same row back on two
    pages, which is exactly what a board somebody is dragging cards around on is.

    Done to the read rather than inside either half that consumes it, because it is a property of
    the read. The draft half guarded itself and the wrapped half did not, and both are read from
    a map built once for the whole board and never written to, so a second copy is judged against
    the state before the first was acted on. For a draft that meant syncing a thread nothing had
    changed; for a wrapped card it meant a second GitHub read of the item on every poll that saw
    it, and the pass counting one move as two.
    """
    once: dict[int, BoardItem] = {}
    for item in items:
        if item.item_id in once:
            logger.info("the board listed the card %r more than once", item.title)
            continue
        once[item.item_id] = item
    return list(once.values())


async def forget_the_mirror(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    repository_id: int,
    card_id: int,
    to: datetime | None,
) -> None:
    """Put the card's timestamp back, so something comes looking again.

    The database half of a sync commits before the Discord half runs, so a refused thread edit
    leaves the card recorded as current and its thread showing the state before the move. Nothing
    else revisits a draft, and the card is only offered again when GitHub's timestamp beats the
    stored one, which the failed sync just made equal. Without this the thread stays wrong until
    somebody edits the card on GitHub.

    Back to nothing when there was nothing stored, rather than left alone. That case used to
    return early, on the grounds that a card with no timestamp or no thread is offered again
    anyway, which describes the row as it was before the sync and not as the sync has just left
    it. A first mirror into a text channel opens the thread and sends the metadata as a second
    call, and a refusal there raises after the thread has been attached to the row that already
    carries the card's timestamp. The row then holds both, so neither of the two escapes applies:
    `_has_moved` compares the card's own timestamp with itself for ever, and Discord keeps an
    empty thread named after a card with no block in it and nothing anywhere revisiting it.

    `to` is the caller's, because the two callers want different things from it. The poller passes
    what was stored, restoring the row as it was. A caller that reaches a card only when it has no
    thread passes None, because the row as it was is a row the poller will skip.
    """
    async with sessionmaker() as session, session.begin():
        await TrackedItemStore(session).forget_mirror(
            repository_id=repository_id,
            object_type=ObjectType.TICKET,
            github_object_id=card_id,
            to=to,
        )
