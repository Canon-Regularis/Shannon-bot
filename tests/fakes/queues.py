from __future__ import annotations

from typing import Any


class InMemoryDeliveryQueue:
    """DeliveryQueue backed by a list, for route tests that do not need a database."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict[str, Any]]] = []

    async def enqueue(self, delivery_id: str, event_type: str, payload: dict[str, Any]) -> bool:
        if any(seen == delivery_id for seen, _, _ in self.enqueued):
            return False
        self.enqueued.append((delivery_id, event_type, payload))
        return True

    @property
    def ids(self) -> list[str]:
        return [delivery_id for delivery_id, _, _ in self.enqueued]
