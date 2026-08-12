from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.webhook_events import WebhookEventStore
from shannon.domain.enums import DeliveryStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    """A queued delivery, detached from the session that read it."""

    id: int
    delivery_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int

    @property
    def action(self) -> str | None:
        action = self.payload.get("action")
        return action if isinstance(action, str) else None


class DeliveryQueue(Protocol):
    """Where deliveries wait between arriving and being acted on."""

    async def enqueue(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool: ...


class WebhookDeliveryQueue:
    """The delivery queue, backed by `webhook_events`.

    Each call runs in its own session. Writing a delivery down has to be visible to a concurrent
    delivery immediately, so it cannot ride along in whatever transaction is doing the work.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def enqueue(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        """Record a delivery, returning False if GitHub has sent it before."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        async with self._sessionmaker() as session, session.begin():
            accepted = await WebhookEventStore(session).enqueue(
                delivery_id=delivery_id,
                event_type=event_type,
                payload_hash=hashlib.sha256(body).hexdigest(),
                payload=payload,
            )
        if not accepted:
            logger.info("delivery %s already seen, skipping", delivery_id)
        return accepted

    async def lease(self, *, limit: int, lease_for: timedelta) -> Sequence[Delivery]:
        async with self._sessionmaker() as session, session.begin():
            rows = await WebhookEventStore(session).lease(limit=limit, lease_for=lease_for)
            # Copied out while the session is open, because the caller works on these long
            # after the transaction that leased them has closed.
            return [
                Delivery(
                    id=row.id,
                    delivery_id=row.github_delivery_id,
                    event_type=row.event_type,
                    payload=row.payload or {},
                    attempts=row.attempts,
                )
                for row in rows
            ]

    async def release(self, deliveries: Sequence[Delivery]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).release([delivery.id for delivery in deliveries])

    async def finish(self, delivery: Delivery, status: DeliveryStatus) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).finish(delivery.id, status)

    async def retry_later(self, delivery: Delivery, *, error: str, delay: timedelta) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).retry_later(delivery.id, error=error, delay=delay)

    async def give_up(self, delivery: Delivery, *, error: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).give_up(delivery.id, error=error)

    async def prune(self, *, keep_for: timedelta) -> int:
        async with self._sessionmaker() as session, session.begin():
            return await WebhookEventStore(session).prune(keep_for=keep_for)
