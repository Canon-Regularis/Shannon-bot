"""Which handler owns which GitHub event.

Apart from `events`, which states what this bot has an opinion about. That list is product
policy and changes when the mirrored behaviour changes; this is the plumbing that carries the
decision out, and changes when registration or dispatch does. Almost everything that answers a
webhook imports `events` to name an outcome, and none of it wants the registry.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shannon.github.webhooks.events import (
    SUPPORTED_EVENTS,
    EventHandler,
    WebhookOutcome,
    is_supported,
)

logger = logging.getLogger(__name__)


class EventRouter:
    """Maps a GitHub event type to the handler that owns it.

    Handlers register themselves at startup, which keeps the HTTP route free of any knowledge
    about what a pull request is.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event: str, handler: EventHandler) -> None:
        if event not in SUPPORTED_EVENTS:
            raise ValueError(f"{event!r} is not a supported webhook event")
        self._handlers[event] = handler

    def handles(self, event: str) -> bool:
        return event in self._handlers

    def will_act_on(self, event: str, action: str | None) -> bool:
        """Whether dispatching this could actually do anything.

        The route asks before recording a delivery. A repository sends pushes, stars and forks
        constantly, and logging every one of them would grow the delivery table without ever
        protecting anything.
        """
        return is_supported(event, action) and event in self._handlers

    async def dispatch(
        self, event: str, action: str | None, payload: Mapping[str, Any]
    ) -> WebhookOutcome:
        if not is_supported(event, action):
            logger.debug("ignoring unsupported webhook %s.%s", event, action)
            return WebhookOutcome.IGNORED

        handler = self._handlers.get(event)
        if handler is None:
            logger.warning("no handler registered for supported event %s", event)
            return WebhookOutcome.IGNORED

        return await handler(action or "", payload)
