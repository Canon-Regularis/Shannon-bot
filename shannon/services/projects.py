"""Mirroring a GitHub project board, by asking it rather than by being told.

Everything else in this bot is delivered: GitHub posts a webhook, the queue writes it down, the
worker acts on it. A project board cannot work that way. GitHub sends `projects_v2` webhooks for
organisation projects only, and never for a personal account's, so for the accounts this is most
likely to run against there is no event to receive at all. The events the requirements name,
`project_card.created` and its siblings, belong to Projects (classic), which GitHub sunset in
August 2024 and removed from Enterprise Server in 3.17.

So this polls. The cost is latency, bounded by the interval. The saving is that a personal board
and an organisation one work on the same code path, with no second webhook to install.

The board answers with every card every time, so the work is deciding which of them moved. That
is one query for what is stored and a comparison in memory, rather than a question per card.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.board import status_from_column
from shannon.domain.enums import ObjectType, Status
from shannon.domain.errors import ShannonError
from shannon.domain.models import RepositorySnapshot, TicketSnapshot
from shannon.domain.time import as_utc
from shannon.services.sync.items import SyncsItems

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BoardItem:
    """One card on a board, as this service needs it.

    Not GitHub's response shape. The client turns whatever GitHub sends into this, so a change
    to their JSON is a change to one parser rather than to any of the logic below.
    """

    item_id: int
    title: str
    column: str | None
    html_url: str
    kind: ObjectType = ObjectType.TICKET
    updated_at: datetime | None = None
    # GitHub's id for the issue or pull request the card wraps, which is the id that item was
    # already stored under when its own webhook arrived. None for a draft, which wraps nothing.
    content_id: int | None = None

    @property
    def is_draft(self) -> bool:
        return self.kind is ObjectType.TICKET


class ReadsBoards(Protocol):
    """Listing what is on a project board, which is all this service asks of GitHub."""

    async def list_board_items(self, owner: str, project_number: int) -> Sequence[BoardItem]: ...


class MovesStatus(Protocol):
    """Setting a tracked item's status, which is what a card moving on a board amounts to.

    The same path a person takes with /set_in_review, deliberately. A board move and a command
    are the same event told two ways, and routing them differently is how the labels on GitHub
    and the block in Discord start disagreeing.
    """

    async def set_status(self, *, thread_id: int, status: Status) -> object: ...


class ProjectPoller:
    """Reads a board on a timer and syncs the cards that have moved since the last read."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        projects: ReadsBoards,
        sync: SyncsItems,
        workflow: MovesStatus,
        *,
        project_number: int,
        interval: float = 60.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._projects = projects
        self._sync = sync
        self._workflow = workflow
        self._project_number = project_number
        self._interval = interval
        self._stopping = False
        self._stopped = asyncio.Event()

    @property
    def enabled(self) -> bool:
        """Whether a board was configured at all. Zero means none."""
        return self._project_number > 0

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()

    async def run_once(self) -> int:
        """Read the board and sync what moved, answering with how many cards that was."""
        if not self.enabled:
            return 0

        board = await self._registered()
        if board is None:
            # Nobody has run /register, so there is no guild to post into and no owner to ask
            # about. Not an error: the process runs before anybody has set it up.
            return 0

        items = await self._projects.list_board_items(board.owner, self._project_number)
        moved = await self._mirror_drafts(board, [i for i in items if i.is_draft])
        moved += await self._move_tracked(board, [i for i in items if not i.is_draft])

        if moved:
            logger.info("mirrored %s of %s cards that had moved", moved, len(items))
        return moved

    async def _mirror_drafts(self, board: _Board, drafts: Sequence[BoardItem]) -> int:
        """A draft card is its own item, so it gets a thread of its own.

        Decided on the card's timestamp, because the card is the thing: if GitHub says it has
        not been touched since the last read, nothing about it can have changed.
        """
        seen = await self._mirrored(board.repository_id)
        mirrored = 0
        for item in drafts:
            if not _has_moved(item, *seen.get(item.item_id, (None, None))):
                continue
            try:
                await self._sync.sync(self._snapshot(board, item))
            except ShannonError as error:
                # One card at a time, the way the wrapped half already does it. Without this a
                # single card Discord refuses takes the rest of the drafts with it and the
                # wrapped half after them, none of which had anything wrong.
                logger.warning("could not mirror the card %r: %s", item.title, error.message)
                continue
            mirrored += 1
        return mirrored

    async def _move_tracked(self, board: _Board, wrapped: Sequence[BoardItem]) -> int:
        """A card wrapping an issue or a pull request moves the thread that item already has.

        Not a second thread and not a second snapshot: the issue is mirrored from its own
        webhooks, and all the board adds is which column it sits in. That goes through the same
        path a person takes with /set_in_review, so the label on GitHub and the block in Discord
        cannot end up disagreeing about a status the board decided.

        Decided by comparing statuses rather than timestamps. A card's `updated_at` and an
        issue's are two different clocks measuring two different things, and a card edited for
        any other reason would otherwise re-assert a status nobody moved.
        """
        moved = 0
        for item in wrapped:
            wanted = status_from_column(item.column)
            if wanted is None or item.content_id is None:
                continue

            tracked = await self._tracked(board.repository_id, item)
            if tracked is None or tracked.thread_id is None or tracked.status is wanted:
                continue

            try:
                await self._workflow.set_status(thread_id=tracked.thread_id, status=wanted)
            except ShannonError as error:
                # A status the item cannot hold, such as anything but DONE on a closed issue.
                # The board is allowed to disagree with GitHub; it is not allowed to win.
                logger.info(
                    "board column %r does not apply to %s: %s",
                    item.column,
                    item.title,
                    error.message,
                )
                continue
            moved += 1
        return moved

    async def _tracked(self, repository_id: int, item: BoardItem) -> _Tracked | None:
        async with self._sessionmaker() as session:
            row = await TrackedItemStore(session).get(
                repository_id=repository_id,
                object_type=item.kind,
                github_object_id=item.content_id,
            )
            return _Tracked(row.discord_thread_id, row.status) if row is not None else None

    async def run_forever(self) -> None:
        """Read the board until asked to stop.

        A failure is logged and waited out rather than ending the loop, for the reason the
        delivery worker does the same: a board that cannot be read this minute is usually
        readable the next, and a poller that dies takes the feature with it until a restart.
        """
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not read the project board, carrying on")
            await self._wait()

    async def _wait(self) -> None:
        """Sleep the interval, or wake at once if a stop arrives.

        Waiting on the sleep alone would leave a shutdown sitting out the whole interval, and at
        a minute that is far longer than the grace period allows.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)

    async def _registered(self) -> _Board | None:
        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).only_one()
            return _Board.of(repository) if repository is not None else None

    async def _mirrored(self, repository_id: int) -> dict[int, tuple[datetime | None, int | None]]:
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).mirrored_state(
                repository_id=repository_id, object_type=ObjectType.TICKET
            )

    def _snapshot(self, board: _Board, item: BoardItem) -> TicketSnapshot:
        return TicketSnapshot(
            repository=board.snapshot,
            github_object_id=item.item_id,
            # A card has no number of its own, so the board's is carried instead. It is what a
            # reader of the row has to go on to find where the thing came from.
            number=self._project_number,
            title=item.title,
            html_url=item.html_url,
            state="open",
            updated_at=item.updated_at,
            action="polled",
            column=item.column,
            project_number=self._project_number,
        )


@dataclass(frozen=True, slots=True)
class _Tracked:
    """What the poller needs of an item that is already mirrored, out of its session."""

    thread_id: int | None
    status: Status


def _has_moved(item: BoardItem, stored: datetime | None, thread_id: int | None) -> bool:
    """Whether a card is worth syncing.

    Strictly newer, because equal means untouched since the last read. The sync path treats
    equal timestamps as current on purpose, which is right for a delivery that may be a retry
    and wrong for a poll that sees the same card every minute.

    A card with no thread is always synced, whatever its timestamp says. The row is written and
    committed before the Discord call that opens the thread, so a card can be recorded as
    current and have nothing to show for it, and comparing timestamps alone would leave it that
    way until somebody happened to touch it on GitHub. Nothing else rescues a draft: an issue
    gets another webhook, a draft has only this.

    A card with no timestamp is always synced too. GitHub gives one, so that is a guard rather
    than a case, and syncing too often is a wasted edit where skipping is a change nobody sees.
    """
    if thread_id is None or item.updated_at is None or stored is None:
        return True
    return as_utc(item.updated_at) > as_utc(stored)


@dataclass(frozen=True, slots=True)
class _Board:
    """The registered repository, as plain values out of its session."""

    repository_id: int
    owner: str
    snapshot: RepositorySnapshot

    @classmethod
    def of(cls, repository: Repository) -> _Board:
        owner, _, name = repository.repo_name.partition("/")
        return cls(
            repository_id=repository.id,
            owner=owner,
            snapshot=RepositorySnapshot(
                github_repo_id=repository.github_repo_id,
                owner=owner,
                name=name,
                html_url=repository.repo_url,
            ),
        )
