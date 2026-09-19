"""Turning a `check_suite` webhook body into the little it usefully says. Issue #112.

A check suite carries its head commit and, when GitHub can work it out, the pull requests that
commit is the head of. That second part is the whole of how a CI result finds a thread, because
nothing in this project can turn a bare SHA into a tracked item.

**A suite with no pull requests is dropped, and pull requests from forks are therefore not
supported.** GitHub leaves that array empty for a fork's head branch, and for a commit on the
default branch. The obvious fix is `GET /repos/{owner}/{repo}/commits/{sha}/pulls`, and it must
not be used: that endpoint answers with pull requests ASSOCIATED with the commit, which includes
the one that was merged to put it there. This repository runs CI on every push to `main`, so
every merge would resolve to the pull request just merged, post into its archived thread, reopen
it, and ring every reviewer of finished work. That is the failure migration `0021` was written to
stop, reached by a different road.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.domain.models import RepositorySnapshot
from shannon.github import mapping
from shannon.github.webhooks.events import CHECK_SUITE_ACTIONS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckSuiteEvent:
    """A finished check suite, reduced to what finding and judging it needs."""

    repository: RepositorySnapshot
    head_sha: str
    # Every pull request this commit heads, not just the first. One branch can be the head of two
    # open pull requests, one into the default branch and one into a release branch, and picking
    # either would be arbitrary. Both threads want the result.
    numbers: tuple[int, ...]


def parse_check_suite_event(action: str, payload: JsonObject) -> CheckSuiteEvent | None:
    """Read a `check_suite` body, or answer None for one there is nothing to do about."""
    if action not in CHECK_SUITE_ACTIONS:
        return None

    repository = mapping.repository(payload.get("repository"))
    if repository is None:
        logger.warning("check_suite.%s arrived without a usable repository", action)
        return None

    suite = payload.get("check_suite")
    if not is_json_object(suite):
        logger.warning("check_suite.%s arrived without a check suite", action)
        return None

    head_sha = suite.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        logger.warning("check_suite.%s arrived without a head commit", action)
        return None

    numbers = _pull_request_numbers(suite.get("pull_requests"))
    if not numbers:
        # Ordinary rather than a fault: a push to a branch with no pull request open, a push to
        # the default branch, or a fork. See the module docstring for why there is no fallback.
        logger.info(
            "a check suite on %s %s heads no pull request, so nothing is said about it",
            repository.full_name,
            head_sha,
        )
        return None

    return CheckSuiteEvent(repository=repository, head_sha=head_sha, numbers=numbers)


def _pull_request_numbers(payload: object) -> tuple[int, ...]:
    """The numbers of the pull requests a suite heads, skipping any row that cannot say."""
    if not is_json_list(payload):
        return ()
    found: list[int] = []
    for row in payload:
        number = row.get("number") if is_json_object(row) else None
        if isinstance(number, int) and number not in found:
            found.append(number)
    return tuple(found)
