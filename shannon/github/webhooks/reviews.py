from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shannon.domain.models import ReviewSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import REVIEW_ACTIONS

logger = logging.getLogger(__name__)


def parse_review_event(action: str, payload: Mapping[str, Any]) -> ReviewSnapshot | None:
    """Turn a `pull_request_review` webhook body into a snapshot.

    The pull request is identified by number, matching how comments are looked up, so both
    reach a tracked item the same way.
    """
    if action not in REVIEW_ACTIONS:
        return None

    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("pull_request_review.%s arrived without a usable repository", action)
        return None

    item = payload.get("pull_request")
    number = item.get("number") if isinstance(item, Mapping) else None
    if not isinstance(number, int):
        logger.warning("pull_request_review.%s arrived without a pull request number", action)
        return None

    review = payload.get("review")
    if not isinstance(review, Mapping):
        logger.warning("pull_request_review.%s arrived without a review", action)
        return None

    review_id = review.get("id")
    if not isinstance(review_id, int):
        return None

    html_url = review.get("html_url")
    body = review.get("body")
    state = review.get("state")
    return ReviewSnapshot(
        repository=repository,
        item_number=number,
        review_id=review_id,
        html_url=html_url if isinstance(html_url, str) else "",
        body=body if isinstance(body, str) else "",
        state=state if isinstance(state, str) else "",
        author=mapping.actor(review.get("user")),
        created_at=mapping.parse_timestamp(review.get("submitted_at")),
    )
