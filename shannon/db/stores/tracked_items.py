from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.domain.enums import CardState, ObjectType, Priority, Status
from shannon.domain.json import JsonObject
from shannon.domain.time import as_utc


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One tracked item as the board poller needs it, out of its session."""

    tracked_item_id: int
    thread_id: int | None
    status: Status
    column: str | None
    # The board card this item is wrapped by, or None until a poll has paired them.
    card_id: int | None = None


@dataclass(frozen=True, slots=True)
class TicketThread:
    """One draft card's row as the poller follows the card itself, out of its session.

    Issue #198. Only rows that still have a thread, because what follows a card is its thread:
    one let go of, by a conversion or a deletion, has nothing left to shut or reopen. The board's
    page comes with it - owner and number both - so the poller can tell this board's cards from a
    board the server used to mirror before it would ask GitHub about one.
    """

    tracked_item_id: int
    card_id: int
    thread_id: int
    state: str
    board_url: str

    @property
    def archived(self) -> bool:
        return self.state == CardState.ARCHIVED


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
                TrackedItem.project_item_id,
            ).where(TrackedItem.repository_id == repository_id)
        )
        return {
            (row[0], row[1]): BoardRow(row[2], row[3], row[4], row[5], row[6]) for row in rows.all()
        }

    async def ticket_threads(self, *, repository_id: int) -> list[TicketThread]:
        """Every draft card of one repository that still has a thread, in one query.

        Issue #198. Asked once per board per poll, because what the poller compares it against - the
        board - is read whole every pass anyway, and a card the listing has stopped showing is found
        here rather than by asking GitHub about every card.
        """
        rows = await self._session.execute(
            select(
                TrackedItem.id,
                TrackedItem.github_object_id,
                TrackedItem.discord_thread_id,
                TrackedItem.github_state,
                TrackedItem.github_url,
            )
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == ObjectType.TICKET,
                TrackedItem.discord_thread_id.is_not(None),
            )
            .order_by(TrackedItem.id)
        )
        return [
            TicketThread(row[0], row[1], thread_id, row[3], row[4])
            for row in rows.all()
            # Narrowed per row rather than trusted from the WHERE clause, which filters in SQL and
            # tells the type checker nothing.
            if (thread_id := row[2]) is not None
        ]

    async def remember_card_state(
        self, tracked_item_id: int, *, thread_id: int, state: CardState
    ) -> None:
        """Record where a draft card now stands on its board. Issue #198.

        Only while the row still points at the thread this was decided about. A relocation can have
        moved the card to a new thread in the meantime, and the state belongs to the card the
        decision was made over, read beside that thread.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(TrackedItem.id == tracked_item_id, TrackedItem.discord_thread_id == thread_id)
            .values(github_state=state.value)
            .execution_options(synchronize_session=False)
        )

    async def remember_board_page(self, tracked_item_id: int, *, page: str) -> None:
        """Keep a draft card's board page as the listing now gives it. Issue #198.

        The page is how the poller tells this board's cards from an old board's, and it names the
        board's owner - so when that account is renamed, rows written before the rename name an
        owner the board no longer has, and a card of theirs that later goes missing would never be
        asked about. A listed card proves its page, so the row takes it.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(TrackedItem.id == tracked_item_id)
            .values(github_url=page)
            .execution_options(synchronize_session=False)
        )

    async def remember_cards(self, pairs: Mapping[int, int]) -> None:
        """Write down which board card wraps each of these items.

        Only the ones whose stored id differs, decided by the caller in memory off a board
        it has already read. A board that has not changed since the last deploy still has to
        record its cards once, and every card on it sits in the branch that returns early
        without writing anything - so this cannot hang off a card having moved.
        """
        for tracked_item_id, card_id in pairs.items():
            await self._session.execute(
                update(TrackedItem)
                .where(TrackedItem.id == tracked_item_id)
                .values(project_item_id=card_id)
            )
        await self._session.flush()

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

    async def forget_the_board(self, repository_id: int) -> None:
        """Let go of everything this repository remembers about the board it was on.

        A card id belongs to the board it is on, so it means nothing the moment the
        repository is pointed at a different one - and it is not merely stale, it is
        wrong in a way that WRITES: the number would be sent as a card id under the
        new board's owner and number, which is either a 404 nobody sees or another
        card entirely.

        The COLUMN goes with it, for exactly the same reason and for a while it did not.
        A column name belongs to its board too, and keeping one across a relink is wrong
        twice over. The poller acts on a card having moved by comparing the listing
        against the column it last saw, so a remembered column from the old board reads
        as a move on the first poll of the new one - and the guard that exists to stop
        the board overwriting a decision somebody made cannot fire, because it only
        arms when no column is remembered at all. And the rule that refuses a move for
        skipping a column measures from that same column, so it would measure from one
        the new board does not have and quietly stop applying.

        Nothing else clears either. The poller only ever writes a pairing, and only for
        cards on the board it just read, so an item absent from the new board would
        keep the old id and the old column for ever.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(TrackedItem.repository_id == repository_id)
            .values(project_item_id=None, project_column=None)
        )
        await self._session.flush()

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

    async def shown_fields(self, *, repository_id: int, card_id: int) -> JsonObject | None:
        """The board fields a reader was last shown for one card, or None for never seen.

        Issue #182. Keyed by the CARD id, which is what a ticket row is keyed by and what the
        poller has in hand - so this needs no tracked item id and no second lookup to find one.

        None and an empty object are different answers and the caller leans on it: None means this
        card's fields have never been recorded, which is how the first poll after this shipped says
        nothing instead of announcing every field of every card at once.

        Read only for a card the board says has moved, so this is one query per CHANGED card rather
        than one per card per poll. A board where nothing moved asks nothing.
        """
        return await self._session.scalar(
            select(TrackedItem.shown_fields).where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == ObjectType.TICKET,
                TrackedItem.github_object_id == card_id,
            )
        )

    async def remember_shown_fields(
        self, *, repository_id: int, card_id: int, fields: JsonObject
    ) -> None:
        """Record the board fields a reader has now been shown for one card.

        Written before the line is posted, never after, and that order is the whole of the
        idempotency here: a poller is a loop rather than a queue, so a Discord refusal after the
        write costs that one announcement, where a refusal before it would say the same thing on
        every poll until somebody fixed the permission. `_hand_over_converted` strikes the same
        bargain and says so at more length.
        """
        await self._session.execute(
            update(TrackedItem)
            .where(
                TrackedItem.repository_id == repository_id,
                TrackedItem.github_object_type == ObjectType.TICKET,
                TrackedItem.github_object_id == card_id,
            )
            .values(shown_fields=fields)
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
