from __future__ import annotations

import hashlib
import logging
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.webhook_events import WebhookEventStore

logger = logging.getLogger(__name__)


class DeliveryStatus(StrEnum):
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"


class DeliveryGuard(Protocol):
    """Decides whether a delivery is new work or a repeat GitHub already sent."""

    async def claim(self, delivery_id: str, event_type: str, body: bytes) -> bool: ...

    async def complete(self, delivery_id: str, status: DeliveryStatus) -> None: ...

    async def release(self, delivery_id: str) -> None: ...


class WebhookIdempotencyGuard:
    """Delivery IDs backed by the webhook_events table.

    Each call runs in its own session. The claim has to be visible to a concurrent delivery
    immediately, so it cannot ride along in whatever transaction the handler is using.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def claim(self, delivery_id: str, event_type: str, body: bytes) -> bool:
        payload_hash = hashlib.sha256(body).hexdigest()
        async with self._sessionmaker() as session, session.begin():
            claimed = await WebhookEventStore(session).claim(
                delivery_id, event_type, payload_hash, DeliveryStatus.PROCESSING
            )
        if not claimed:
            logger.info("delivery %s already seen, skipping", delivery_id)
        return claimed

    async def complete(self, delivery_id: str, status: DeliveryStatus) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).mark(delivery_id, status)

    async def release(self, delivery_id: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await WebhookEventStore(session).release(delivery_id)
