from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from shannon.domain.models import PullRequestSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import PULL_REQUEST_ACTIONS

logger = logging.getLogger(__name__)

REVIEW_REQUESTED = "review_requested"


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

    if action == REVIEW_REQUESTED:
        return _with_event_reviewer(snapshot, payload)
    return snapshot


def _with_event_reviewer(
    snapshot: PullRequestSnapshot, payload: Mapping[str, Any]
) -> PullRequestSnapshot:
    """Fold `review_requested`'s top-level reviewer into the reviewer list.

    GitHub puts whoever was just added at the top level of the event, as `requested_reviewer` for
    a person and `requested_team` for a team, and their appearance in the list on the pull request
    is not guaranteed. Both are folded in, because a review asked of a team is a review asked.

    Only ever called for `review_requested`. `review_request_removed` carries the same field
    holding the person who was just taken off, and folding that in would put them straight
    back.
    """
    requested = mapping.actor(payload.get("requested_reviewer")) or mapping.team(
        payload.get("requested_team")
    )
    if requested is None or any(r.login == requested.login for r in snapshot.reviewers):
        return snapshot

    return replace(snapshot, reviewers=(*snapshot.reviewers, requested))
