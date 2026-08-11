from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shannon.domain.models import IssueSnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import ISSUE_ACTIONS

logger = logging.getLogger(__name__)


def parse_issue_event(action: str, payload: Mapping[str, Any]) -> IssueSnapshot | None:
    """Turn an `issues` webhook body into the same snapshot the REST client produces.

    Returns None when the action is out of scope or the body is missing something the sync path
    cannot work without, which callers read as nothing to do rather than as a failure.
    """
    if action not in ISSUE_ACTIONS:
        return None

    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("issues.%s arrived without a usable repository", action)
        return None

    # `issues` events are never sent for pull requests, so unlike the REST path there is
    # nothing to filter out here.
    snapshot = mapping.issue(payload.get("issue"), repository, action=action)
    if snapshot is None:
        logger.warning("issues.%s arrived without a usable issue", action)
        return None

    return snapshot
