from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import Interval, cast, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from shannon.db.models import WebhookEvent
from shannon.domain.enums import DeliveryStatus


def _interval(value: timedelta) -> ColumnElement[timedelta]:
    """A timedelta as something the database can add to a timestamp."""
    return cast(literal(value), Interval)


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
        payload: dict[str, Any],
    ) -> bool:
        """Write a delivery down, returning False if it was already here.

        The insert carries its own conflict handling so two deliveries racing on the same id
        cannot both win. A read-then-write check would let both through.
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

    async def _revive(self, delivery_id: str, payload: dict[str, Any]) -> bool:
        """Put a delivery that was given up on back on the queue, reporting whether it moved.

        GitHub's Redeliver button reuses the delivery id, so without this a FAILED delivery just
        reads as a duplicate and nothing happens. Any other state is left alone: a repeat of one
        already processed is still a duplicate.
        """
        result = await self._session.execute(
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
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def lease(self, *, limit: int, lease_for: timedelta) -> Sequence[WebhookEvent]:
        """Take up to `limit` deliveries to work on, in the order they arrived.

        `SKIP LOCKED` means a second worker picks up different rows rather than blocking, so
        running more than one stays correct even though only one runs today.

        A payload is required, which excludes rows written before this table became a queue.
        A row still PROCESSING past its lease is taken back, which is how work belonging to a
        worker that died gets retried instead of sitting there forever.
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
        # Every deadline in this table is written and read against the database clock; setting
        # one from the application clock would have a worker on a drifted host hold its lease
        # for longer or shorter than it believes.
        claimed = (
            await self._session.scalars(
                update(WebhookEvent)
                .where(WebhookEvent.id.in_(eligible))
                .values(status=DeliveryStatus.PROCESSING, locked_until=now + _interval(lease_for))
                .returning(WebhookEvent)
                .execution_options(synchronize_session=False)
            )
        ).all()

        # RETURNING makes no promise about order, and the order is the point.
        return sorted(claimed, key=lambda row: row.id)

    async def release(self, event_ids: Sequence[int]) -> None:
        """Hand leased deliveries back without counting an attempt against them.

        Nothing was tried, so this is not a failure. Leaving them locked instead would keep the
        replacement process from touching them until the lease ran out.
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
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(status=status, processed_at=func.now(), locked_until=None, last_error=None)
        )

    async def retry_later(self, event_id: int, *, error: str, delay: timedelta) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                status=DeliveryStatus.PENDING,
                attempts=WebhookEvent.attempts + 1,
                next_attempt_at=func.now() + _interval(delay),
                locked_until=None,
                last_error=error,
            )
        )

    async def give_up(self, event_id: int, *, error: str) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
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

        The bodies hold issue titles and comment text from private repositories, so they do not
        sit here indefinitely. Anything still pending is left alone however old it is.
        """
        result = await self._session.execute(
            delete(WebhookEvent)
            .where(
                WebhookEvent.status.in_(DeliveryStatus.terminal()),
                WebhookEvent.processed_at < func.now() - _interval(keep_for),
            )
            # Without this the ORM cannot work out which loaded objects the DELETE hit, so it
            # asks the database to hand every deleted primary key back. Nothing here holds
            # those rows in a session, so there is nothing to synchronise.
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0
