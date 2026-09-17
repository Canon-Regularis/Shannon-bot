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
        # A push to the branch. Listed last because it is the only one of these that is not about
        # the item's own fields: everything above changes what the metadata block says, and this
        # one changes nothing there at all. It is here so the thread can say what landed on the
        # pull request, which is issue #67.
        #
        # This is the largest single increase in queue volume the project has taken. Every push
        # to every open pull request now writes a row carrying the whole payload, where before it
        # was matched and dropped at the endpoint. GitHub sends one of these per push rather than
        # per commit, and the pruner clears them after seven days, so it is bounded rather than
        # growing, but it is a real change in what the queue holds.
        "synchronize",
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

# Somebody installing, uninstalling, pausing or resuming the App, and somebody adding or removing
# repositories from an existing installation. Not about an item at all, which is what makes these
# different from everything above: they are the only events that change what this bot is ABLE to
# see rather than what it has been told.
#
# GitHub delivers them whether or not they are ticked in the App's settings, so listing them here
# is about acting on them rather than about receiving them.
INSTALLATION_ACTIONS = frozenset(
    {"created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"}
)
INSTALLATION_REPOSITORY_ACTIONS = frozenset({"added", "removed"})

SUPPORTED_EVENTS: Mapping[str, frozenset[str]] = {
    "pull_request": PULL_REQUEST_ACTIONS,
    "issues": ISSUE_ACTIONS,
    "issue_comment": COMMENT_ACTIONS,
    "pull_request_review": REVIEW_ACTIONS,
    "installation": INSTALLATION_ACTIONS,
    "installation_repositories": INSTALLATION_REPOSITORY_ACTIONS,
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
