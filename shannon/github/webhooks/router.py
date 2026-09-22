"""Which handler owns which GitHub event.

Separate from `events`, which states what this bot has an opinion about: that list is product
policy, and this is the plumbing that carries the decision out.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from shannon.domain.json import JsonObject
from shannon.github.webhooks.events import (
    SUPPORTED_EVENTS,
    EventHandler,
    WebhookOutcome,
    is_supported,
)

logger = logging.getLogger(__name__)

# Whether a delivery of an event this bot handles is worth writing down at all. Asked
# only where the event table cannot settle it, which is where one event type covers
# both the deliveries that can reach an item and the ones that never could.
WorthRecording = Callable[[JsonObject], bool]


class EventRouter:
    """Maps a GitHub event type to the handler that owns it.

    Handlers register themselves at startup, so the HTTP route knows nothing about pull requests.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, EventHandler] = {}
        self._worth_recording: dict[str, WorthRecording] = {}

    def register(
        self,
        event: str,
        handler: EventHandler,
        *,
        worth_recording: WorthRecording | None = None,
    ) -> None:
        if event not in SUPPORTED_EVENTS:
            raise ValueError(f"{event!r} is not a supported webhook event")
        self._handlers[event] = handler
        if worth_recording is not None:
            self._worth_recording[event] = worth_recording

    def handles(self, event: str) -> bool:
        return event in self._handlers

    def will_act_on(self, event: str, action: str | None, payload: JsonObject) -> bool:
        """Whether dispatching this could actually do anything.

        The route asks before recording a delivery, because a repository sends pushes, stars and
        forks constantly and logging every one would grow the delivery table without protecting
        anything.

        The payload is read only where an event is registered with a question about it. Most are
        settled by the table alone; `check_suite` is not, because GitHub sends one for every
        branch running CI and only the ones naming a pull request can reach a thread.
        """
        if not (is_supported(event, action) and event in self._handlers):
            return False
        worth_recording = self._worth_recording.get(event)
        return worth_recording is None or worth_recording(payload)

    async def dispatch(
        self,
        event: str,
        action: str | None,
        payload: JsonObject,
        arrived: int | None = None,
    ) -> WebhookOutcome:
        if not is_supported(event, action):
            logger.debug("ignoring unsupported webhook %s.%s", event, action)
            return WebhookOutcome.IGNORED

        handler = self._handlers.get(event)
        if handler is None:
            logger.warning("no handler registered for supported event %s", event)
            return WebhookOutcome.IGNORED

        return await handler(action or "", payload, arrived)
