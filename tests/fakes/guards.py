from __future__ import annotations

from shannon.services.idempotency import DeliveryStatus


class InMemoryDeliveryGuard:
    """DeliveryGuard backed by a set, for tests that do not need a database."""

    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.completed: dict[str, DeliveryStatus] = {}
        self.released: list[str] = []

    async def claim(self, delivery_id: str, event_type: str, body: bytes) -> bool:
        if delivery_id in self.claimed:
            return False
        self.claimed.add(delivery_id)
        return True

    async def complete(self, delivery_id: str, status: DeliveryStatus) -> None:
        self.completed[delivery_id] = status

    async def release(self, delivery_id: str) -> None:
        self.claimed.discard(delivery_id)
        self.released.append(delivery_id)
