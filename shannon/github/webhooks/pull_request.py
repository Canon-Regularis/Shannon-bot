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
    """Fold `review_requested`'s top-level reviewer into the reviewer list, and record it.

    GitHub puts whoever was just added at the top level of the event, as `requested_reviewer` for
    a person and `requested_team` for a team, and their appearance in the list on the pull request
    is not guaranteed. Both are folded in, because a review asked of a team is a review asked.

    Kept as well as folded in. Whether the list changed cannot answer "was this asked again": a
    team GitHub silently dropped when a member reviewed is back in the list by the time the
    re-request arrives, so the payload is identical to the one that asked the first time. The
    top-level name is what says which party this event is about.

    Only ever called for `review_requested`. `review_request_removed` carries the same field
    holding the person who was just taken off, and folding that in would put them straight
    back.
    """
    person = mapping.actor(payload.get("requested_reviewer"))
    if person is not None:
        reviewers = snapshot.reviewers
        if not any(r.login == person.login for r in reviewers):
            reviewers = (*reviewers, person)
        return replace(snapshot, reviewers=reviewers, person_asked_now=person)

    asked = mapping.team(payload.get("requested_team"))
    if asked is None:
        return snapshot

    teams = snapshot.reviewer_teams
    if not any(t.login == asked.login for t in teams):
        teams = (*teams, asked)
    return replace(snapshot, reviewer_teams=teams, team_asked_now=asked)
