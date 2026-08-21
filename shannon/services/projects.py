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

from shannon.db.models import COLUMN_WIDTH, TITLE_WIDTH, URL_WIDTH, Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.board import normalise, status_from_column
from shannon.domain.enums import ObjectType, Status
from shannon.domain.errors import ShannonError
from shannon.domain.models import RepositorySnapshot, TicketSnapshot
from shannon.domain.time import as_utc
from shannon.github.errors import GitHubRateLimitError
from shannon.github.projects import BoardItem
from shannon.services.sync.items import SyncsItems
from shannon.services.workflow import WorkflowRefusedError

logger = logging.getLogger(__name__)

# The longest GitHub's own primary rate limit window runs, which resets hourly. A `retry-after`
# past that is a header nobody meant, and sitting one out would take the feature off for the rest
# of the day on the strength of a number nothing here can check.
RATE_LIMIT_CEILING = 3600


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
        done: set[int] = set()
        for item in drafts:
            if item.item_id in done:
                # A board is read a page at a time by cursor, and a cursor is not a snapshot:
                # GitHub says outright that a list edited while it is being paged through can
                # hand the same row back on two pages, which is what a board somebody is
                # dragging cards around on is. `seen` is read once for the whole board and not
                # written to, so the second copy still reads as unmirrored and syncs again,
                # rewriting a thread nothing had changed.
                logger.info("the board listed the card %r more than once", item.title)
                continue
            done.add(item.item_id)

            stored, thread_id = seen.get(item.item_id, (None, None))
            if not _has_moved(item, stored, thread_id):
                continue
            try:
                result = await self._sync.sync(self._snapshot(board, item))
            except ShannonError as error:
                # One card at a time, the way the wrapped half already does it. Without this a
                # single card Discord refuses takes the rest of the drafts with it and the
                # wrapped half after them, none of which had anything wrong.
                logger.warning("could not mirror the card %r: %s", item.title, error.message)
                await self._forget_the_mirror(board, item, stored)
                continue
            except Exception:
                # Anything the sync path did not expect, a card too wide for its column being
                # the one that got here first. Logged whole, because a surprise is a defect and
                # the traceback is what says where; caught, because `run_forever` swallows it
                # identically and takes every card after this one with it, on every poll, for
                # as long as the process lives.
                logger.exception("could not mirror the card %r", item.title)
                await self._forget_the_mirror(board, item, stored)
                continue

            # What the sync did, not merely that it returned. A repository with no channel
            # mapped for tickets answers with NOT_TRACKED on every card of every poll, and
            # counting those reports a board being mirrored while nothing is written.
            if result.synced:
                mirrored += 1
        return mirrored

    async def _forget_the_mirror(
        self, board: _Board, item: BoardItem, stored: datetime | None
    ) -> None:
        """Put the card's timestamp back to what it was before the sync that failed.

        The database half of a sync commits before the Discord half runs, so a refused thread
        edit leaves the card recorded as current and its thread showing the state before the
        move. Nothing else revisits a draft, and the card is only offered again when GitHub's
        timestamp beats the stored one, which the failed sync just made equal. Without this the
        thread stays wrong until somebody edits the card on GitHub.

        Nothing to put back when there was nothing stored: a card with no timestamp, or with no
        thread, is offered again anyway.
        """
        if stored is None:
            return
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).forget_mirror(
                repository_id=board.repository_id,
                object_type=ObjectType.TICKET,
                github_object_id=item.item_id,
                to=stored,
            )

    async def _move_tracked(self, board: _Board, wrapped: Sequence[BoardItem]) -> int:
        """A card wrapping an issue or a pull request moves the thread that item already has.

        Not a second thread and not a second snapshot: the issue is mirrored from its own
        webhooks, and all the board adds is which column it sits in. That goes through the same
        path a person takes with /set_in_review, so the label on GitHub and the block in Discord
        cannot end up disagreeing about a status the board decided.

        Acting on a MOVE, not on a disagreement. Those are different questions and answering the
        wrong one is what made the board win every argument: a reviewer setting a pull request to
        ready for merge, on a card still sitting in `In Progress`, had the decision reverted
        within the interval, silently, because all the poller could see was that the two did not
        match. It compares against the column it last saw instead, so a card nobody has touched
        says nothing at all.
        """
        moved = 0
        for item in wrapped:
            try:
                moved += await self._move_one(board, item)
            except Exception:
                # The same bargain the draft half makes, for the same reason. Everything below
                # is per-card already; this is only about the failures nobody wrote a branch
                # for, which would otherwise end the poll and repeat for ever.
                logger.exception("could not move the card %r", item.title)
        return moved

    async def _move_one(self, board: _Board, item: BoardItem) -> int:
        """Act on one card, answering with whether it moved anything."""
        if item.content_id is None:
            return 0

        tracked = await self._tracked(board.repository_id, item)
        if tracked is None or tracked.thread_id is None:
            return 0

        column = _fits(item.column)
        if _same_column(column, tracked.column):
            if tracked.column is None:
                # Nothing moved and nothing was ever seen look the same here, and a card added
                # to a board carries no Status until somebody picks one, so this is what most
                # cards look like on the poll that first meets them. Passing over without
                # writing the column down leaves it null, null means never seen, and the
                # first-look guard below is still armed when a Status is finally set: the move
                # that sets it is read as a first look and dropped, and the column matches from
                # then on so no later poll revisits it.
                await self._remember_column(tracked.tracked_item_id, column)
            return 0

        wanted = status_from_column(column)
        if wanted is None or (wanted is tracked.status and tracked.column is None):
            # A column nobody has taught us, or the first look at a card that already agrees
            # with its item. Neither is a move to carry out, and both have to be written down
            # or the same card is looked at again on every poll for ever.
            await self._remember_column(tracked.tracked_item_id, column)
            return 0

        if tracked.column is None and tracked.status is not Status.NOT_REVIEWED:
            # First sight of this card. The board fills in an item nobody has said anything
            # about; it does not get to overwrite a decision somebody already made, because
            # from here the two are indistinguishable and only one of them was deliberate.
            logger.info(
                "leaving %s at %s: the board says %r but this is the first look at its card",
                item.title,
                tracked.status.value,
                column,
            )
            await self._remember_column(tracked.tracked_item_id, column)
            return 0

        # A card that has moved before goes through even where the status already matches.
        # Setting a status is several steps and the stored one is written in the middle of them:
        # a card dragged to Done whose thread Discord then refused to lock comes back here with
        # the status already DONE and the lock still owed, and skipping on that reads the half
        # that succeeded as the whole. The column is the record of a move having been carried
        # through, and it says this one was not. Repeating a status nothing changed costs one
        # read of the item and writes nothing, which is what makes it safe to send round again.
        try:
            await self._workflow.set_status(thread_id=tracked.thread_id, status=wanted)
        except WorkflowRefusedError as refusal:
            # A status the item cannot hold, such as anything but DONE on a closed issue.
            # The board is allowed to disagree with GitHub; it is not allowed to win. This is
            # a final answer rather than a bad moment, so the move is written off as seen and
            # the same complaint is not made again on every poll for ever.
            logger.info(
                "board column %r does not apply to %s: %s",
                column,
                item.title,
                refusal.message,
            )
            await self._remember_column(tracked.tracked_item_id, column)
            return 0
        except ShannonError as error:
            # GitHub or Discord having a bad moment, which is not an answer about anything.
            # The column is deliberately NOT recorded: remembering it here would mark the
            # move as seen while it never happened, and since nothing else ever rederives a
            # status from a board, the card would sit in its new column for ever with the
            # old status and no poll would look at it again.
            logger.warning("could not move %s to %s: %s", item.title, wanted.value, error)
            return 0

        await self._remember_column(tracked.tracked_item_id, column)
        return 1

    async def _remember_column(self, tracked_item_id: int, column: str) -> None:
        """Record where the card was, storing the empty string for a card with no column at all.

        Null has to keep meaning one thing, and it already means never seen. Writing null for a
        card whose Status somebody cleared would put it back to never seen, which re-arms the
        first-look guard and quietly drops the next real move.
        """
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_column(tracked_item_id, column)

    async def _tracked(self, repository_id: int, item: BoardItem) -> _Tracked | None:
        async with self._sessionmaker() as session:
            row = await TrackedItemStore(session).get(
                repository_id=repository_id,
                object_type=item.kind,
                github_object_id=item.content_id,
            )
            if row is None:
                return None
            return _Tracked(row.id, row.discord_thread_id, row.status, row.project_column)

    async def run_forever(self) -> None:
        """Read the board until asked to stop.

        A failure is logged and waited out rather than ending the loop, for the reason the
        delivery worker does the same: a board that cannot be read this minute is usually
        readable the next, and a poller that dies takes the feature with it until a restart.
        """
        while not self._stopping:
            wait = self._interval
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except GitHubRateLimitError as limit:
                # GitHub answers a spent limit with the moment the window reopens, and reading
                # the board again inside that window cannot succeed. On the interval alone this
                # is a full board read a minute for as long as the limit lasts, which is worse
                # than wasted: GitHub lengthens a secondary limit for requests made during one.
                # The longer of the two, so a header asking for no wait cannot talk the poller
                # out of its own interval.
                wait = max(wait, min(limit.retry_after or 0, RATE_LIMIT_CEILING))
                logger.warning(
                    "GitHub's rate limit is spent, waiting %ss before reading the board again",
                    int(wait),
                )
            except Exception:
                logger.exception("could not read the project board, carrying on")
            await self._wait(wait)

    async def _wait(self, seconds: float) -> None:
        """Sleep, or wake at once if a stop arrives.

        Waiting on the sleep alone would leave a shutdown sitting out the whole interval, and at
        a minute that is far longer than the grace period allows.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)

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
            # Cut to what the row holds. A draft card's Title is a free text field with no cap
            # on GitHub's side, unlike an issue's, and one card too wide for the column ends the
            # whole poll rather than that one card.
            title=item.title[:TITLE_WIDTH],
            html_url=item.html_url[:URL_WIDTH],
            state="open",
            updated_at=item.updated_at,
            action="polled",
            column=item.column,
            project_number=self._project_number,
        )


@dataclass(frozen=True, slots=True)
class _Tracked:
    """What the poller needs of an item that is already mirrored, out of its session."""

    tracked_item_id: int
    thread_id: int | None
    status: Status
    column: str | None


def _fits(column: str | None) -> str:
    """The card's column, cut to what the row will hold, and never null.

    A board's Status is whatever somebody typed into a field named Status, and the field does
    not have to be a single select at all: the poller matches it by name. A value wider than the
    row raises out of the flush, past the per-card handling, and stalls the board behind that
    one card. Cutting here rather than at the write is what keeps the comparison honest, since
    what is compared next poll is what was stored.
    """
    return (column or "")[:COLUMN_WIDTH]


def _same_column(seen: str | None, remembered: str | None) -> bool:
    """Whether a card is where the last poll left it.

    Compared the way the column is read: trimmed and case-folded, so a board renaming `Done` to
    `done` is not a move and does not restate a status nobody changed.
    """
    return normalise(seen or "") == normalise(remembered or "")


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
