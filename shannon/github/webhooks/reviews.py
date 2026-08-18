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

    snapshot = mapping.review(payload.get("review"), repository, item_number=number)
    if snapshot is None:
        logger.warning("pull_request_review.%s arrived without a usable review", action)
    return snapshot
