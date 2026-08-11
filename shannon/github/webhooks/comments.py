from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shannon.domain.enums import ObjectType
from shannon.domain.models import CommentSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import COMMENT_ACTIONS

logger = logging.getLogger(__name__)


def parse_comment_event(action: str, payload: Mapping[str, Any]) -> CommentSnapshot | None:
    """Turn an `issue_comment` webhook body into a snapshot.

    GitHub sends this event for pull requests as well as issues, with the pull request
    represented as an issue, so nothing here filters on which kind it is.
    """
    if action not in COMMENT_ACTIONS:
        return None

    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("issue_comment.%s arrived without a usable repository", action)
        return None

    item = payload.get("issue")
    number = item.get("number") if isinstance(item, Mapping) else None
    if not isinstance(number, int):
        logger.warning("issue_comment.%s arrived without an item number", action)
        return None

    comment = payload.get("comment")
    if not isinstance(comment, Mapping):
        logger.warning("issue_comment.%s arrived without a comment", action)
        return None

    comment_id = comment.get("id")
    if not isinstance(comment_id, int):
        return None

    html_url = comment.get("html_url")
    body = comment.get("body")
    return CommentSnapshot(
        repository=repository,
        item_number=number,
        comment_id=comment_id,
        html_url=html_url if isinstance(html_url, str) else "",
        body=body if isinstance(body, str) else "",
        author=mapping.actor(comment.get("user")),
        created_at=mapping.parse_timestamp(comment.get("created_at")),
        # The `pull_request` key is how GitHub distinguishes the two here.
        object_type=ObjectType.PR if mapping.is_pull_request(item) else ObjectType.ISSUE,
    )
