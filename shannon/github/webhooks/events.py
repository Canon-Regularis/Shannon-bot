from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from shannon.domain.json import JsonObject, is_json_object
from shannon.domain.models import RepositorySnapshot
from shannon.github import mapping

logger = logging.getLogger(__name__)

# Anything not listed here is matched and dropped rather than erroring, starting with the ping
# GitHub sends the moment the webhook is configured. Removals are listed alongside the additions
# they undo: with only one half, a thread goes on claiming a label that was taken off.
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
        # A push to the branch: the only action here that changes nothing in the metadata block.
        # It is listed so the thread can say what landed. GitHub sends one per push rather than
        # per commit, and the pruner clears the rows after seven days.
        "synchronize",
        # The two halves of the draft switch, listed together for the reason above. A draft is
        # coloured differently and rings nobody, so with only one half a pull request keeps the
        # colour and the silence of whichever state it was last told about. Issue #132.
        "ready_for_review",
        "converted_to_draft",
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

# `dismissed` and `edited` reviews are not mirrored, for the reason the comment actions give.
REVIEW_ACTIONS = frozenset({"submitted"})

# One inline comment on the diff. GitHub sends one of these per comment and a
# `pull_request_review` wrapping the lot, so a review round costs one delivery more than the
# number of notes in it.
REVIEW_COMMENT_ACTIONS = frozenset({"created"})

# Only a suite that has finished. `requested` and `rerequested` say CI has STARTED, and acting on
# either would announce an empty result. This fires for a push to ANY branch running CI, not only
# one with an open pull request, so most deliveries are answered "nothing tracked here".
CHECK_SUITE_ACTIONS = frozenset({"completed"})

# Somebody installing, uninstalling, pausing or resuming the App, or adding and removing
# repositories from an installation. These are the only events that change what this bot is ABLE
# to see. GitHub delivers them whether or not they are ticked in the App's settings.
INSTALLATION_ACTIONS = frozenset(
    {"created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"}
)
INSTALLATION_REPOSITORY_ACTIONS = frozenset({"added", "removed"})

SUPPORTED_EVENTS: Mapping[str, frozenset[str]] = {
    "pull_request": PULL_REQUEST_ACTIONS,
    "issues": ISSUE_ACTIONS,
    "issue_comment": COMMENT_ACTIONS,
    "pull_request_review": REVIEW_ACTIONS,
    "pull_request_review_comment": REVIEW_COMMENT_ACTIONS,
    "check_suite": CHECK_SUITE_ACTIONS,
    "installation": INSTALLATION_ACTIONS,
    "installation_repositories": INSTALLATION_REPOSITORY_ACTIONS,
}


class WebhookOutcome(StrEnum):
    # What the endpoint answers: nothing has reached Discord at that point.
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    # What a handler answers once the worker runs it.
    PROCESSED = "processed"


class EventHandler(Protocol):
    """Handles one GitHub event type. Implementations live in the services layer."""

    async def __call__(
        self, action: str, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        """Act on one delivery of this event type.

        `arrived` is the number the queue gave the delivery, which is the order it reached this
        bot. The item sync uses it to place two deliveries carrying the same `updated_at`, which
        GitHub stamps to the second so they routinely do. None where there is no delivery behind
        the call.
        """
        ...


def is_supported(event: str, action: str | None) -> bool:
    actions = SUPPORTED_EVENTS.get(event)
    if actions is None:
        return False
    return action in actions


def repository_of(event: str, action: str, payload: JsonObject) -> RepositorySnapshot | None:
    """The repository a webhook body names, or None having said it was missing."""
    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("%s.%s arrived without a usable repository", event, action)
    return repository


def repository_and_number(
    event: str, action: str, payload: JsonObject, *, item_key: str, noun: str
) -> tuple[RepositorySnapshot, int] | None:
    """The repository and item number a note event names, or None having said what was missing.

    `item_key` is `issue` for a comment and `pull_request` for a review: GitHub represents a pull
    request as an issue on the comment event and as itself on the other two.
    """
    repository = repository_of(event, action, payload)
    if repository is None:
        return None

    item = payload.get(item_key)
    number = item.get("number") if is_json_object(item) else None
    if not isinstance(number, int):
        logger.warning("%s.%s arrived without %s", event, action, noun)
        return None
    return repository, number
