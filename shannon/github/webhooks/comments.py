from __future__ import annotations

import logging

from shannon.domain.json import JsonObject, is_json_object
from shannon.domain.models import CommentSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import COMMENT_ACTIONS

logger = logging.getLogger(__name__)


def parse_comment_event(action: str, payload: JsonObject) -> CommentSnapshot | None:
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
    number = item.get("number") if is_json_object(item) else None
    if not isinstance(number, int):
        logger.warning("issue_comment.%s arrived without an item number", action)
        return None

    snapshot = mapping.comment(payload.get("comment"), repository, item_number=number, on=item)
    if snapshot is None:
        logger.warning("issue_comment.%s arrived without a usable comment", action)
    return snapshot
