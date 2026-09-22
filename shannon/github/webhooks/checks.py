"""Turning a `check_suite` webhook body into the little it usefully says.

A check suite carries its head commit and, when GitHub can work it out, the pull requests that
commit heads. Nothing here can turn a bare SHA into a tracked item, so that array is the whole of
how a CI result finds a thread. GitHub leaves it empty for a fork's head branch and for a commit
on the default branch, so such a suite is dropped and pull requests from forks are not supported.
The obvious fallback, `GET /repos/{owner}/{repo}/commits/{sha}/pulls`, must not be used: it
answers with pull requests ASSOCIATED with the commit, including the one merged to put it there,
so CI on every push to `main` would reopen the merged pull request's archived thread and ring
every reviewer of finished work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.domain.models import RepositorySnapshot
from shannon.github.webhooks.events import CHECK_SUITE_ACTIONS, repository_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckSuiteEvent:
    """A finished check suite, reduced to what finding and judging it needs."""

    repository: RepositorySnapshot
    head_sha: str
    # Every pull request this commit heads, not just the first: one branch can be the head of an
    # open pull request into the default branch and another into a release branch, and both
    # threads want the result.
    numbers: tuple[int, ...]


def parse_check_suite_event(action: str, payload: JsonObject) -> CheckSuiteEvent | None:
    """Read a `check_suite` body, or answer None for one there is nothing to do about."""
    if action not in CHECK_SUITE_ACTIONS:
        return None

    repository = repository_of("check_suite", action, payload)
    if repository is None:
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
        # the default branch, or a fork.
        logger.info(
            "a check suite on %s %s heads no pull request, so nothing is said about it",
            repository.full_name,
            head_sha,
        )
        return None

    return CheckSuiteEvent(repository=repository, head_sha=head_sha, numbers=numbers)


def heads_a_pull_request(payload: JsonObject) -> bool:
    """Whether a check suite names a pull request there could be a thread for.

    Asked at the route, before the delivery is written down. The parser below drops the same
    suites, but by then the body is a row: a suite is around 25kB of JSONB, it is kept for the
    retention window, and a repository with CI on a protected default branch sends one per push
    and merge. None of those could ever have been acted on.

    Only the array is read, not the head commit or the repository, because a suite with no
    pull requests in it is dropped whatever else is wrong with it.
    """
    suite = payload.get("check_suite")
    return is_json_object(suite) and bool(_pull_request_numbers(suite.get("pull_requests")))


def _pull_request_numbers(payload: object) -> tuple[int, ...]:
    if not is_json_list(payload):
        return ()
    found: list[int] = []
    for row in payload:
        number = row.get("number") if is_json_object(row) else None
        if isinstance(number, int) and number not in found:
            found.append(number)
    return tuple(found)
