from __future__ import annotations

import logging

from shannon.domain.json import JsonObject
from shannon.domain.models import ReviewCommentSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import REVIEW_COMMENT_ACTIONS, repository_and_number

logger = logging.getLogger(__name__)


def parse_review_comment_event(action: str, payload: JsonObject) -> ReviewCommentSnapshot | None:
    """Turn a `pull_request_review_comment` webhook body into a snapshot."""
    if action not in REVIEW_COMMENT_ACTIONS:
        return None

    found = repository_and_number(
        "pull_request_review_comment",
        action,
        payload,
        item_key="pull_request",
        noun="a pull request number",
    )
    if found is None:
        return None
    repository, number = found

    snapshot = mapping.review_comment(payload.get("comment"), repository, item_number=number)
    if snapshot is None:
        logger.warning("pull_request_review_comment.%s arrived without a usable comment", action)
    return snapshot
