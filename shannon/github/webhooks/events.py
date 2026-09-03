from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

# Anything not listed here arrives from GitHub the moment the webhook is configured, starting
# with the ping it sends to prove the endpoint answers, and is matched and dropped rather
# than erroring. Project events belong to MVP 4.
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


class WebhookOutcome(StrEnum):
    # What the endpoint answers: the delivery is written down and will be acted on behind the
    # response. Nothing has reached Discord yet at that point.
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    # What a handler answers once the worker runs it.
    PROCESSED = "processed"


class EventHandler(Protocol):
    """Handles one GitHub event type. Implementations live in the services layer."""

    async def __call__(
        self, action: str, payload: Mapping[str, Any], arrived: int | None = None
    ) -> WebhookOutcome:
        """`arrived` is the number the queue gave this delivery, which is the order it reached
        this bot. Handlers that have no use for it ignore it; the item sync uses it to place two
        deliveries carrying the same `updated_at`, which GitHub stamps to the second so they
        routinely do. None where there is no delivery behind the call.
        """
        ...


def is_supported(event: str, action: str | None) -> bool:
    actions = SUPPORTED_EVENTS.get(event)
    if actions is None:
        return False
    return action in actions
