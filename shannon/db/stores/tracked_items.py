from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, update
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


@dataclass(frozen=True, slots=True)
class StrandedThread:
    """One item whose thread may be in a channel nothing maps any more, out of its session.

    `channel_id` is None where the row does not remember, which is every thread claimed before
    that column existed.
    """

    tracked_item_id: int
    object_type: ObjectType
    number: int
    thread_id: int
    channel_id: int | None


class TrackedItemStore:
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

        Two syncs of one item overlap by design. `lock` makes Postgres re-read the row once it
        grants the lock, so the second caller in decides against what the first wrote. Off by
        default: every other caller reads to answer a question and writes nothing.
        """
        statement = select(TrackedItem).where(
            TrackedItem.repository_id == repository_id,
            TrackedItem.github_object_type == object_type,
            TrackedItem.github_object_id == github_object_id,
        )
        found: TrackedItem | None = await self._session.scalar(
            statement.with_for_update() if lock else statement
        )
        return found

    async def get_by_id(self, tracked_item_id: int, *, lock: bool = False) -> TrackedItem | None:
        """Find one item by its own id, optionally holding it for the rest of the transaction."""
        return await self._session.get(TrackedItem, tracked_item_id, with_for_update=lock)

    async def get_with_its_server(self, tracked_item_id: int) -> tuple[TrackedItem, int] | None:
        """One item together with the Discord server its thread lives in.

        The repository is a foreign key and names exactly one server, so one read answers both.
        The caller that needs it tells a bot that has been removed apart from a permission it was
        never given, and has no snapshot left to carry the server down from.
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
        value already stale by commit time; GREATEST compares at write time and ignores nulls.
        Lowering the mark blinds the staleness guard and the lock step, which both read it.
        """
        item.github_updated_at = func.greatest(TrackedItem.github_updated_at, as_utc(incoming))

    async def get_by_thread(self, discord_thread_id: int) -> TrackedItem | None:
        """Find the item a Discord thread belongs to.

        For the workflow commands, which take no argument and act on the thread they are run in.
        Nothing else looks an item up this way, so the column carries no index: one row per
        thread and a handful of commands a day.
        """
        found: TrackedItem | None = await self._session.scalar(
            select(TrackedItem).where(TrackedItem.discord_thread_id == discord_thread_id)
        )
        return found

    async def get_by_number(
        self, *, repository_id: int, number: int, object_type: ObjectType
    ) -> TrackedItem | None:
        """Find an item by its GitHub number and kind.

        By number because `issue_comment` payloads report the issue id even for a pull request,
        and that never matches the pull request id stored against the tracked item; the number
        matches for both. Issues and pull requests share one numbering sequence per repository.
        """
        found: TrackedItem | None = await self._session.scalar(
            select(TrackedItem)
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_number == number,
                TrackedItem.github_object_type == object_type,
            )
            .order_by(TrackedItem.id)
        )
        return found

    async def mirrored_state(
        self, *, repository_id: int, object_type: ObjectType
    ) -> dict[int, tuple[datetime | None, int | None]]:
        """When each item of one kind was last seen, and whether it reached Discord.

        For the poller, which is handed a whole board every minute and works out which of it
        moved. The thread is here because a row is committed before the Discord call that gives
        it a thread, so an item can be recorded as current while nobody can see it.
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

        One query per board rather than one per card, because a board is read whole every minute
        whether or not any card moved.
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

    async def stranded_threads(
        self, *, repository_id: int, kind: ObjectType, channel_id: int
    ) -> list[StrandedThread]:
        """Items of this kind whose thread is not known to be in `channel_id`.

        A candidate list, not an answer: a row remembering no channel is included, and only
        asking Discord settles where the thread is. One kind rather than several, because
        `/set_channel` gives a kind borrowing another's channel a row of its own before it
        moves anything, so no other kind's destination changed (#134). Ordered by what moved
        most recently, because a run is capped.
        """
        rows = await self._session.execute(
            select(
                TrackedItem.id,
                TrackedItem.github_object_type,
                TrackedItem.github_object_number,
                TrackedItem.discord_thread_id,
                TrackedItem.discord_channel_id,
            )
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == kind,
                TrackedItem.discord_thread_id.is_not(None),
                or_(
                    TrackedItem.discord_channel_id.is_(None),
                    TrackedItem.discord_channel_id != channel_id,
                ),
            )
            .order_by(TrackedItem.github_updated_at.desc().nullslast(), TrackedItem.id.desc())
        )
        return [StrandedThread(row[0], row[1], row[2], row[3], row[4]) for row in rows.all()]

    async def remember_column(self, tracked_item_id: int, column: str) -> None:
        """Record the board column this item was last seen in.

        Written whether or not the column was acted on: it answers "has the board moved since we
        looked", which has to stay true for a move the item could not take, or the same refusal
        repeats on every poll for ever.
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

        GitHub sends most things again, and drafts are the one thing it does not: a card comes
        back to the poller only when its timestamp is newer than the stored one, and the failed
        sync just made those equal. `None` puts it back to never seen, for a card that failed on
        its very first mirror.
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

        GitHub fires several events at once for a newly opened item, each with its own delivery
        id, so the duplicate guard lets all of them through and check-then-insert would leave all
        but one failing on the unique constraint. The row comes back locked because every caller
        writes to it, and on a conflict the insert has already waited out the other transaction.
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
