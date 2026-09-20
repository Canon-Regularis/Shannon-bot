from __future__ import annotations

import logging

from shannon.domain.json import JsonObject
from shannon.domain.models import CommentSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import COMMENT_ACTIONS, repository_and_number

logger = logging.getLogger(__name__)


def parse_comment_event(action: str, payload: JsonObject) -> CommentSnapshot | None:
    """Turn an `issue_comment` webhook body into a snapshot.

    GitHub sends this event for pull requests as well as issues, with the pull request
    represented as an issue, so nothing here filters on which kind it is.
    """
    if action not in COMMENT_ACTIONS:
        return None

    found = repository_and_number(
        "issue_comment", action, payload, item_key="issue", noun="an item number"
    )
    if found is None:
        return None
    repository, number = found

    snapshot = mapping.comment(
        payload.get("comment"), repository, item_number=number, on=payload.get("issue")
    )
    if snapshot is None:
        logger.warning("issue_comment.%s arrived without a usable comment", action)
    return snapshot
