from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)

Payload = Mapping[str, Any]


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def actor(payload: Any) -> Actor | None:
    """GitHub sends `null` for a deleted account, so this has to tolerate a missing object."""
    if not isinstance(payload, Mapping):
        return None
    login = payload.get("login")
    if not isinstance(login, str) or not login:
        return None
    github_user_id = payload.get("id")
    return Actor(
        login=login, github_user_id=github_user_id if isinstance(github_user_id, int) else None
    )


def actors(payloads: Any) -> tuple[Actor, ...]:
    if not isinstance(payloads, Iterable) or isinstance(payloads, str | bytes | Mapping):
        return ()
    parsed = (actor(item) for item in payloads)
    return tuple(item for item in parsed if item is not None)


def labels(payloads: Any) -> tuple[Label, ...]:
    if not isinstance(payloads, Iterable) or isinstance(payloads, str | bytes | Mapping):
        return ()

    result: list[Label] = []
    for item in payloads:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            colour = item.get("color")
            result.append(Label(name=name, color=colour if isinstance(colour, str) else None))
    return tuple(result)


def repository(payload: Any) -> RepositorySnapshot | None:
    if not isinstance(payload, Mapping):
        return None

    repo_id = payload.get("id")
    name = payload.get("name")
    if not isinstance(repo_id, int) or not isinstance(name, str) or not name:
        return None

    owner = _owner_login(payload)
    if owner is None:
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"https://github.com/{owner}/{name}"

    return RepositorySnapshot(github_repo_id=repo_id, owner=owner, name=name, html_url=html_url)


def _owner_login(payload: Payload) -> str | None:
    owner = actor(payload.get("owner"))
    if owner is not None:
        return owner.login

    # Some payloads carry only full_name, so fall back to splitting it.
    full_name = payload.get("full_name")
    if isinstance(full_name, str) and "/" in full_name:
        return full_name.split("/", 1)[0]
    return None


def issue(
    payload: Any, repo: RepositorySnapshot, *, action: str | None = None
) -> IssueSnapshot | None:
    """Build a snapshot from an issue object.

    The same shape comes back from `GET /repos/{owner}/{repo}/issues/{number}` and rides inside
    `issues` webhook payloads.
    """
    if not isinstance(payload, Mapping):
        return None

    object_id = payload.get("id")
    number = payload.get("number")
    if not isinstance(object_id, int) or not isinstance(number, int):
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"{repo.html_url}/issues/{number}"

    title = payload.get("title")
    state = payload.get("state")
    return IssueSnapshot(
        repository=repo,
        github_object_id=object_id,
        number=number,
        title=title if isinstance(title, str) else "",
        html_url=html_url,
        state=state if isinstance(state, str) else "open",
        author=actor(payload.get("user")),
        assignees=actors(payload.get("assignees")),
        labels=labels(payload.get("labels")),
        updated_at=parse_timestamp(payload.get("updated_at")),
        closed_at=parse_timestamp(payload.get("closed_at")),
        action=action,
    )


def is_pull_request(payload: Any) -> bool:
    """Whether an issue-shaped payload is really a pull request.

    GitHub serves pull requests from the issues endpoint too, and marks them only with this
    key. Without the check, `/issue` pointed at a pull request number would track it a second
    time under the wrong type.
    """
    return isinstance(payload, Mapping) and payload.get("pull_request") is not None


def pull_request(
    payload: Any, repo: RepositorySnapshot, *, action: str | None = None
) -> PullRequestSnapshot | None:
    """Build a snapshot from a pull request object.

    The same object shape comes back from `GET /repos/{owner}/{repo}/pulls/{number}` and rides
    inside `pull_request` webhook payloads, so both callers land here.
    """
    if not isinstance(payload, Mapping):
        return None

    object_id = payload.get("id")
    number = payload.get("number")
    title = payload.get("title")
    if not isinstance(object_id, int) or not isinstance(number, int):
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"{repo.html_url}/pull/{number}"

    state = payload.get("state")
    return PullRequestSnapshot(
        repository=repo,
        github_object_id=object_id,
        number=number,
        title=title if isinstance(title, str) else "",
        html_url=html_url,
        state=state if isinstance(state, str) else "open",
        author=actor(payload.get("user")),
        assignees=actors(payload.get("assignees")),
        reviewers=actors(payload.get("requested_reviewers")),
        labels=labels(payload.get("labels")),
        merged=bool(payload.get("merged")) or payload.get("merged_at") is not None,
        updated_at=parse_timestamp(payload.get("updated_at")),
        action=action,
    )
