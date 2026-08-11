from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from shannon.domain.models import PullRequestSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import PULL_REQUEST_ACTIONS

logger = logging.getLogger(__name__)


def parse_pull_request_event(action: str, payload: Mapping[str, Any]) -> PullRequestSnapshot | None:
    """Turn a `pull_request` webhook body into the same snapshot the REST client produces.

    Returns None when the action is out of scope or the body is missing something the sync
    path cannot work without. Callers treat None as "nothing to do" rather than as a failure,
    because GitHub sends plenty of events this bot has no opinion about.
    """
    if action not in PULL_REQUEST_ACTIONS:
        return None

    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("pull_request.%s arrived without a usable repository", action)
        return None

    snapshot = mapping.pull_request(payload.get("pull_request"), repository, action=action)
    if snapshot is None:
        logger.warning("pull_request.%s arrived without a usable pull request", action)
        return None

    return _with_event_reviewer(snapshot, payload)


def _with_event_reviewer(
    snapshot: PullRequestSnapshot, payload: Mapping[str, Any]
) -> PullRequestSnapshot:
    """Fold `review_requested`'s top-level reviewer into the reviewer list.

    GitHub puts the person just added at the top level of the event, and their appearance in
    `pull_request.requested_reviewers` is not guaranteed for team requests.
    """
    requested = mapping.actor(payload.get("requested_reviewer"))
    if requested is None or any(r.login == requested.login for r in snapshot.reviewers):
        return snapshot

    return replace(snapshot, reviewers=(*snapshot.reviewers, requested))
