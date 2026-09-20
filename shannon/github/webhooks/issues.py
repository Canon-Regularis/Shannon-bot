from __future__ import annotations

import logging

from shannon.domain.json import JsonObject
from shannon.domain.models import IssueSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import ISSUE_ACTIONS, repository_of

logger = logging.getLogger(__name__)


def parse_issue_event(action: str, payload: JsonObject) -> IssueSnapshot | None:
    """Turn an `issues` webhook body into the same snapshot the REST client produces.

    Returns None when the action is out of scope or the body is missing something the sync path
    cannot work without, which callers read as nothing to do rather than as a failure.
    """
    if action not in ISSUE_ACTIONS:
        return None

    repository = repository_of("issues", action, payload)
    if repository is None:
        return None

    # `issues` events are never sent for pull requests, so unlike the REST path there is
    # nothing to filter out here.
    snapshot = mapping.issue(payload.get("issue"), repository, action=action)
    if snapshot is None:
        logger.warning("issues.%s arrived without a usable issue", action)
        return None

    return snapshot
