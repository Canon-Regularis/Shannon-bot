from __future__ import annotations

import logging
from dataclasses import replace

from shannon.domain.json import JsonObject
from shannon.domain.models import PullRequestSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import PULL_REQUEST_ACTIONS, repository_of

logger = logging.getLogger(__name__)

REVIEW_REQUESTED = "review_requested"


def parse_pull_request_event(action: str, payload: JsonObject) -> PullRequestSnapshot | None:
    """Turn a `pull_request` webhook body into the same snapshot the REST client produces.

    None means the action is out of scope or the body is unusable. Callers treat it as nothing to
    do rather than as a failure, because GitHub sends plenty of events this bot has no opinion
    about.
    """
    if action not in PULL_REQUEST_ACTIONS:
        return None

    repository = repository_of("pull_request", action, payload)
    if repository is None:
        return None

    snapshot = mapping.pull_request(payload.get("pull_request"), repository, action=action)
    if snapshot is None:
        logger.warning("pull_request.%s arrived without a usable pull request", action)
        return None

    if action == REVIEW_REQUESTED:
        return _with_event_reviewer(snapshot, payload)
    return snapshot


def _with_event_reviewer(snapshot: PullRequestSnapshot, payload: JsonObject) -> PullRequestSnapshot:
    """Fold `review_requested`'s top-level reviewer into the reviewer list, and record it.

    GitHub puts whoever was just added at the top level, as `requested_reviewer` for a person and
    `requested_team` for a team, and their appearance in the list on the pull request is not
    guaranteed. The name is kept as well as folded in, because a team GitHub silently dropped when
    a member reviewed is back in the list by the time the re-request arrives, so the list cannot
    say which party this event is about. Never called for `review_request_removed`, whose same
    field holds the person just taken off; folding that in would put them straight back.
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
