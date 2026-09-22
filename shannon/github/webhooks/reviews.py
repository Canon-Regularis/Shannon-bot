from __future__ import annotations

import logging

from shannon.domain.json import JsonObject
from shannon.domain.models import ReviewSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import REVIEW_ACTIONS, repository_and_number

logger = logging.getLogger(__name__)


def parse_review_event(action: str, payload: JsonObject) -> ReviewSnapshot | None:
    """Turn a `pull_request_review` webhook body into a snapshot."""
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
    return snapshot
