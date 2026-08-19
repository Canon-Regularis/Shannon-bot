from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shannon.github.webhooks.events import WebhookOutcome


class RecordingHandler:
    """Stands in for a service so route tests stay about HTTP.

    Here rather than in tests/support because it implements EventHandler, and the conformance
    check only looks at this package.
    """

    def __init__(self, outcome: WebhookOutcome = WebhookOutcome.PROCESSED) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(self, action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        self.calls.append((action, payload))
        return self.outcome
