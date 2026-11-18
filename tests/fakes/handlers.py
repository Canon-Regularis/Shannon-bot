from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shannon.github.webhooks.events import WebhookOutcome


class RecordingHandler:
    """Stands in for a service so route tests stay about HTTP.

    Here rather than in tests/support because it implements EventHandler: the fakes the
    conformance table checks against their protocols live in tests/fakes, and that table lists
    each one by hand.
    """

    def __init__(self, outcome: WebhookOutcome = WebhookOutcome.PROCESSED) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        # The order each delivery reached this bot, which only the queue can say.
        self.arrivals: list[int | None] = []

    async def __call__(
        self, action: str, payload: Mapping[str, Any], arrived: int | None = None
    ) -> WebhookOutcome:
        self.calls.append((action, payload))
        self.arrivals.append(arrived)
        return self.outcome
