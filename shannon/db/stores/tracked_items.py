from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.domain.time import as_utc


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One tracked item as the board poller needs it, out of its session."""

    tracked_item_id: int
    thread_id: int | None
    status: Status
    column: str | None


class TrackedItemStore:
    """Data access for synced GitHub objects."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        repository_id: int,
        object_type: ObjectType,
        github_object_id: int,
        lock: bool = False,
    ) -> TrackedItem | None:
        """Find one item, optionally holding it for the rest of the transaction.

        `lock` is for the caller that reads the item, decides something from what it read, and
        then writes. Two syncs of one item overlap by design, and without it both read the row
        before either commits, so both decide against the same out-of-date answer. Postgres
        re-reads a row once it grants the lock, so the second one in sees what the first wrote
        and decides against that instead.

        Off by default. Every other caller reads to answer a question and writes nothing, and a
        lock held on the busiest row of the sync path for the length of those is a queue nobody
        asked for.
        """
        statement = select(TrackedItem).where(
            TrackedItem.repository_id == repository_id,
            TrackedItem.github_object_type == object_type,
            TrackedItem.github_object_id == github_object_id,
        )
        return await self._session.scalar(statement.with_for_update() if lock else statement)

    async def get_by_id(self, tracked_item_id: int, *, lock: bool = False) -> TrackedItem | None:
        """Find one item by its own id, optionally holding it for the rest of the transaction.

        `lock` is for the same caller `get`'s is: one that reads the row, decides something from
        what it read, and then writes. Off by default, because most callers here are answering a
        question and writing nothing.
        """
        return await self._session.get(TrackedItem, tracked_item_id, with_for_update=lock)

    async def get_with_its_server(self, tracked_item_id: int) -> tuple[TrackedItem, int] | None:
        """One item together with the Discord server its thread lives in.

        In one read rather than two, because a caller holding the item and not the server has to
        invent an answer for a row that cannot exist: the repository is a foreign key and every
        registered repository names exactly one server. Asked for by the one caller that has to
        tell a refusal from a bot that has been removed apart from a permission it was never
        given, and reaches that point with no snapshot left to carry the server down from.
        """
        row = (
            await self._session.execute(
                select(TrackedItem, Repository.discord_guild_id).where(
                    TrackedItem.id == tracked_item_id,
                    Repository.id == TrackedItem.repository_id,
                )
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else None

    def raise_updated_at(self, item: TrackedItem, incoming: datetime) -> None:
        """Move the item's high-water mark up, and never down.

        Two syncs of one item overlap by design, so a comparison in Python decides against a
        value already stale by commit time. GREATEST compares against the row as it stands at
        write time. Nulls are ignored, covering an item that has never carried a timestamp.

        Lowering the mark blinds the staleness guard to the next delivery too, and the lock step
        reads this same field to decide whether it has been superseded.

        Staged, not executed: it flushes with the item's other changes in one statement.
        """
        item.github_updated_at = func.greatest(TrackedItem.github_updated_at, as_utc(incoming))

    async def get_by_thread(self, discord_thread_id: int) -> TrackedItem | None:
        """Find the item a Discord thread belongs to.

        The workflow commands take no argument and act on the thread they are run in, because
        the thread is the only thing the person running one is looking at. Nothing else looks
        an item up this way, which is why the column carries no index: one row per thread and a
        handful of commands a day is not a query worth an index to maintain on the sync path.
        """
        return await self._session.scalar(
            select(TrackedItem).where(TrackedItem.discord_thread_id == discord_thread_id)
        )

    async def get_by_number(
        self, *, repository_id: int, number: int, object_type: ObjectType
    ) -> TrackedItem | None:
        """Find an item by its GitHub number and kind.

        By number because `issue_comment` payloads report the issue id even for a pull request,
        and that never matches the pull request id stored against the tracked item. The number
        matches for both.

        The kind is required rather than optional. Issues and pull requests share one numbering
        sequence per repository, so a number already identifies exactly one of them, and every
        caller knows which it is asking about; leaving it out would only be a way to be handed
        the other one.
        """
        return await self._session.scalar(
            select(TrackedItem)
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_number == number,
                TrackedItem.github_object_type == object_type,
            )
            .order_by(TrackedItem.id)
        )

    async def mirrored_state(
        self, *, repository_id: int, object_type: ObjectType
    ) -> dict[int, tuple[datetime | None, int | None]]:
        """When each item of one kind was last seen, and whether it reached Discord.

        For the poller, which is handed a whole board every minute and has to work out which of
        it moved. Three columns rather than whole rows, and one query rather than one per card:
        the answer is compared and thrown away, and a board can be long.

        The thread is here because the timestamp alone is not enough to decide. A row is written
        and committed before the Discord call that gives it a thread, so an item can be recorded
        as current while nobody can see it, and a poller comparing only timestamps would never
        look at it again.
        """
        rows = await self._session.execute(
            select(
                TrackedItem.github_object_id,
                TrackedItem.github_updated_at,
                TrackedItem.discord_thread_id,
            ).where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == object_type,
            )
        )
        return {row[0]: (row[1], row[2]) for row in rows.all()}

    async def board_state(self, *, repository_id: int) -> dict[tuple[ObjectType, int], BoardRow]:
        """What the poller needs of every item a board card could wrap, in one query.

        The draft half of a poll already reads its state this way. The wrapped half asked per
        card, which is a session and a query each, once a minute, for every card on the board
        whether or not it had moved. A board is read whole every time, so the number of questions
        should be a function of the number of boards and not of the number of cards.

        Keyed by the pair a card is matched on, because issues and pull requests number
        separately and a board holds both.
        """
        rows = await self._session.execute(
            select(
                TrackedItem.github_object_type,
                TrackedItem.github_object_id,
                TrackedItem.id,
                TrackedItem.discord_thread_id,
                TrackedItem.status,
                TrackedItem.project_column,
            ).where(TrackedItem.repository_id == repository_id)
        )
        return {(row[0], row[1]): BoardRow(row[2], row[3], row[4], row[5]) for row in rows.all()}

    async def remember_column(self, tracked_item_id: int, column: str) -> None:
        """Record the board column this item was last seen in.

        Written whether or not the column was acted on. What it answers is "has the board moved
        since we looked", and that has to be true even when the move was one the item could not
        take, or the same refusal repeats on every poll for ever.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(TrackedItem.id == tracked_item_id)
            .values(project_column=column)
            .execution_options(synchronize_session=False)
        )

    async def forget_mirror(
        self,
        *,
        repository_id: int,
        object_type: ObjectType,
        github_object_id: int,
        to: datetime | None,
    ) -> None:
        """Put the high-water mark back, for a sync that wrote the row and then failed.

        The row is committed before the Discord call that shows it, so a refused thread edit
        leaves the item recorded as current with nothing to show for it. That is recoverable
        for anything GitHub sends again, and drafts are the one thing it does not: a card only
        comes back to the poller when its timestamp is newer than the stored one, and the failed
        sync just made those equal. Lowering the mark is what puts the card back in the queue.

        Deliberately not `raise_updated_at`, which exists to stop the mark moving backwards.
        This is the one caller that means to.

        `None` puts it back to never seen, which is where a card that failed on its very first
        mirror belongs. Anything else would be a moment invented here, and the column already
        has a word for having no answer.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == object_type,
                TrackedItem.github_object_id == github_object_id,
            )
            .values(github_updated_at=as_utc(to) if to is not None else None)
            .execution_options(synchronize_session=False)
        )

    async def get_or_create(
        self,
        *,
        repository_id: int,
        object_type: ObjectType,
        github_object_id: int,
        github_object_number: int,
        github_url: str,
        title: str,
        github_state: str,
        status: Status,
        priority: Priority = Priority.UNSET,
        github_updated_at: datetime | None = None,
    ) -> TrackedItem:
        """Insert the item, or return the one another delivery inserted first.

        GitHub fires several events at once for a newly opened item, and they carry different
        delivery ids so the duplicate guard passes all of them through. Checking for the row
        and then inserting it would let every one of them past the check and leave all but one
        failing on the unique constraint, so the insert carries its own conflict handling.

        The row comes back locked either way, because every caller of this writes to it. On a
        conflict the insert has already waited on the unique index until the other caller
        committed, and the read below would otherwise hand back a row that caller is free to
        keep changing while this one decides things from it.
        """
        statement = (
            pg_insert(TrackedItem)
            .values(
                repository_id=repository_id,
                github_object_type=object_type,
                github_object_id=github_object_id,
                github_object_number=github_object_number,
                github_url=github_url,
                title=title,
                github_state=github_state,
                status=status,
                priority=priority,
                github_updated_at=github_updated_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    TrackedItem.repository_id,
                    TrackedItem.github_object_type,
                    TrackedItem.github_object_id,
                ]
            )
            .returning(TrackedItem.id)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()

        item = (
            await self.get_by_id(inserted)
            if inserted is not None
            else await self.get(
                repository_id=repository_id,
                object_type=object_type,
                github_object_id=github_object_id,
                lock=True,
            )
        )
        if item is None:
            raise RuntimeError(
                f"tracked item for {object_type.value} {github_object_id} vanished mid-write"
            )
        return item
