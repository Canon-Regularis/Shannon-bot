from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import interval, rows_changed
from shannon.db.models import WebhookEvent
from shannon.domain.enums import DeliveryStatus
from shannon.domain.json import JsonObject


class WebhookEventStore:
    """Data access for the delivery queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        delivery_id: str,
        event_type: str,
        payload_hash: str,
        payload: JsonObject,
    ) -> bool:
        """Write a delivery down, returning False if it was already here.

        The insert handles the conflict itself, because a read-then-write check would let two
        deliveries racing on the same id both through.
        """
        statement = (
            pg_insert(WebhookEvent)
            .values(
                github_delivery_id=delivery_id,
                event_type=event_type,
                payload_hash=payload_hash,
                payload=payload,
                status=DeliveryStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.github_delivery_id])
            .returning(WebhookEvent.id)
        )
        if (await self._session.execute(statement)).scalar_one_or_none() is not None:
            return True

        return await self._revive(delivery_id, payload)

    async def _revive(self, delivery_id: str, payload: JsonObject) -> bool:
        """Put a delivery that was given up on back on the queue, reporting whether it moved.

        GitHub's Redeliver button reuses the delivery id, so without this a FAILED delivery reads
        as a duplicate and nothing happens. Any other state is left alone.
        """
        changed = await rows_changed(
            self._session,
            update(WebhookEvent)
            .where(
                WebhookEvent.github_delivery_id == delivery_id,
                WebhookEvent.status == DeliveryStatus.FAILED,
            )
            .values(
                status=DeliveryStatus.PENDING,
                payload=payload,
                attempts=0,
                next_attempt_at=None,
                locked_until=None,
                last_error=None,
            )
            .execution_options(synchronize_session=False),
        )
        return bool(changed)

    async def lease(self, *, limit: int, lease_for: timedelta) -> Sequence[WebhookEvent]:
        """Take up to `limit` deliveries to work on, in the order they arrived.

        `SKIP LOCKED` means a second worker picks up different rows rather than blocking. A
        payload is required, which excludes rows written before this table became a queue, and a
        row still PROCESSING past its lease is taken back, so work belonging to a worker that
        died is retried instead of sitting there forever.
        """
        now = func.now()
        eligible = (
            select(WebhookEvent.id)
            .where(
                WebhookEvent.payload.is_not(None),
                or_(
                    (WebhookEvent.status == DeliveryStatus.PENDING)
                    & (
                        WebhookEvent.next_attempt_at.is_(None)
                        | (WebhookEvent.next_attempt_at <= now)
                    ),
                    (WebhookEvent.status == DeliveryStatus.PROCESSING)
                    & WebhookEvent.locked_until.is_not(None)
                    & (WebhookEvent.locked_until <= now),
                ),
            )
            .order_by(WebhookEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )

        # Claiming in the same statement that selects, so nothing can slip between the two.
        # Every deadline in this table is written and read against the database clock; one set
        # from the application clock would drift against the lease its worker believes it holds.
        claimed = (
            await self._session.scalars(
                update(WebhookEvent)
                .where(WebhookEvent.id.in_(eligible))
                .values(status=DeliveryStatus.PROCESSING, locked_until=now + interval(lease_for))
                .returning(WebhookEvent)
                .execution_options(synchronize_session=False)
            )
        ).all()

        # RETURNING makes no promise about order, and the order is the point.
        return sorted(claimed, key=lambda row: row.id)

    async def release(self, event_ids: Sequence[int]) -> None:
        """Hand leased deliveries back without counting an attempt against them.

        Leaving them locked would keep the replacement process from touching them until the lease
        ran out.
        """
        if not event_ids:
            return
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id.in_(event_ids), WebhookEvent.status == DeliveryStatus.PROCESSING)
            .values(status=DeliveryStatus.PENDING, locked_until=None)
            .execution_options(synchronize_session=False)
        )

    async def finish(self, event_id: int, status: DeliveryStatus) -> None:
        """Record the outcome, on the row this worker still holds.

        Guarded on PROCESSING the way `release` is. Unreachable today, because a lease is
        fifteen minutes and a batch cannot outlive its own: `Settings._lease_fits_a_batch`
        refuses a configuration where it could. Shorten the lease past that guard and this is
        a worker writing an outcome onto a delivery another replica has already taken.
        """
        await self._session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id == event_id,
                WebhookEvent.status == DeliveryStatus.PROCESSING,
            )
            .values(status=status, processed_at=func.now(), locked_until=None, last_error=None)
        )

    async def retry_later(self, event_id: int, *, error: str, delay: timedelta) -> None:
        """Send it round again, on the row this worker still holds. See `finish`."""
        await self._session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id == event_id,
                WebhookEvent.status == DeliveryStatus.PROCESSING,
            )
            .values(
                status=DeliveryStatus.PENDING,
                attempts=WebhookEvent.attempts + 1,
                next_attempt_at=func.now() + interval(delay),
                locked_until=None,
                last_error=error,
            )
        )

    async def give_up(self, event_id: int, *, error: str) -> None:
        """Stop trying, on the row this worker still holds. See `finish`."""
        await self._session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id == event_id,
                WebhookEvent.status == DeliveryStatus.PROCESSING,
            )
            .values(
                status=DeliveryStatus.FAILED,
                attempts=WebhookEvent.attempts + 1,
                processed_at=func.now(),
                locked_until=None,
                last_error=error,
            )
        )

    async def prune(self, *, keep_for: timedelta) -> int:
        """Drop finished deliveries older than `keep_for`.

        The bodies hold issue titles and comment text from private repositories. Anything still
        pending is left alone however old it is.
        """
        changed = await rows_changed(
            self._session,
            delete(WebhookEvent)
            .where(
                WebhookEvent.status.in_(DeliveryStatus.terminal()),
                WebhookEvent.processed_at < func.now() - interval(keep_for),
            )
            # Without this the ORM asks the database to hand back every deleted primary key
            # so it can tell which loaded objects the DELETE hit. Nothing here holds those rows.
            .execution_options(synchronize_session=False),
        )
        return changed or 0
