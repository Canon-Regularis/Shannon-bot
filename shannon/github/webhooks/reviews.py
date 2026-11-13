from __future__ import annotations

import logging
from dataclasses import replace

from shannon.domain.json import JsonObject, is_json_object
from shannon.domain.models import ReviewSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import REVIEW_ACTIONS, repository_and_number

logger = logging.getLogger(__name__)


def parse_review_event(action: str, payload: JsonObject) -> ReviewSnapshot | None:
    """Turn a `pull_request_review` webhook body into a snapshot.

    The pull request's author is read here rather than in `mapping.review`, which also reads the
    REST list of reviews, whose rows carry no pull request to read one off. Issue #263: a review
    asking for changes names whoever opened the pull request, and the webhook already says who that
    is, so nothing has to ask GitHub for the pull request to find out.
    """
    if action not in REVIEW_ACTIONS:
        return None

    found = repository_and_number(
        "pull_request_review",
        action,
        payload,
        item_key="pull_request",
        noun="a pull request number",
    )
    if found is None:
        return None
    repository, number = found

    snapshot = mapping.review(payload.get("review"), repository, item_number=number)
    if snapshot is None:
        logger.warning("pull_request_review.%s arrived without a usable review", action)
        return None

    pull = payload.get("pull_request")
    opener = mapping.actor(pull.get("user")) if is_json_object(pull) else None
    return replace(snapshot, pull_request_author=opener)
