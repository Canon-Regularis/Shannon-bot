from __future__ import annotations

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import WebhookEvent


class WebhookEventStore:
    """Data access for the delivery log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self, delivery_id: str, event_type: str, payload_hash: str, status: str
    ) -> bool:
        """Record a delivery, returning False if it was already recorded.

        The insert carries its own conflict handling so two workers racing on the same delivery
        cannot both win. A read-then-write check would let both through.
        """
        statement = (
            pg_insert(WebhookEvent)
            .values(
                github_delivery_id=delivery_id,
                event_type=event_type,
                payload_hash=payload_hash,
                status=status,
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.github_delivery_id])
            .returning(WebhookEvent.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def mark(self, delivery_id: str, status: str) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.github_delivery_id == delivery_id)
            .values(status=status, processed_at=func.now())
        )

    async def release(self, delivery_id: str) -> None:
        """Drop the record so a retry of a failed delivery is not mistaken for a duplicate."""
        await self._session.execute(
            delete(WebhookEvent).where(WebhookEvent.github_delivery_id == delivery_id)
        )
