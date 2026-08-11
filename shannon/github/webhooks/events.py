from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Anything not listed here arrives from GitHub the moment the webhook is configured, and is
# matched and dropped rather than erroring. Project events belong to MVP 4.
# Removals are handled alongside the additions they undo. Listing only one half would leave a
# thread claiming someone is still assigned, or still holding a label that was taken off, until
# some later event happened to correct it.
PULL_REQUEST_ACTIONS = frozenset(
    {
        "opened",
        "edited",
        "closed",
        "reopened",
        "review_requested",
        "review_request_removed",
        "labeled",
        "unlabeled",
        "assigned",
        "unassigned",
    }
)

ISSUE_ACTIONS = frozenset(
    {
        "opened",
        "edited",
        "closed",
        "reopened",
        "labeled",
        "unlabeled",
        "assigned",
        "unassigned",
    }
)

# Edits and deletions are not mirrored, so a comment in Discord is a record of what was said
# when it was said.
COMMENT_ACTIONS = frozenset({"created"})

# `dismissed` and `edited` reviews are not mirrored, for the same reason comment edits are not:
# the thread records what was said when it was said.
REVIEW_ACTIONS = frozenset({"submitted"})

SUPPORTED_EVENTS: Mapping[str, frozenset[str]] = {
    "pull_request": PULL_REQUEST_ACTIONS,
    "issues": ISSUE_ACTIONS,
    "issue_comment": COMMENT_ACTIONS,
    "pull_request_review": REVIEW_ACTIONS,
}

# GitHub sends this once when a webhook is created. It carries no action and needs no work.
PING_EVENT = "ping"


class WebhookOutcome(StrEnum):
    PROCESSED = "processed"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"


class EventHandler(Protocol):
    """Handles one GitHub event type. Implementations live in the services layer."""

    async def __call__(self, action: str, payload: Mapping[str, Any]) -> WebhookOutcome: ...


def is_supported(event: str, action: str | None) -> bool:
    actions = SUPPORTED_EVENTS.get(event)
    if actions is None:
        return False
    return action in actions


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

    async def dispatch(
        self, event: str, action: str | None, payload: Mapping[str, Any]
    ) -> WebhookOutcome:
        if event == PING_EVENT:
            return WebhookOutcome.IGNORED

        if not is_supported(event, action):
            logger.debug("ignoring unsupported webhook %s.%s", event, action)
            return WebhookOutcome.IGNORED

        handler = self._handlers.get(event)
        if handler is None:
            logger.warning("no handler registered for supported event %s", event)
            return WebhookOutcome.IGNORED

        return await handler(action or "", payload)
