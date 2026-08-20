from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from shannon.domain.enums import ObjectType
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewSnapshot,
)
from shannon.domain.time import as_utc

Payload = Mapping[str, Any]


def parse_timestamp(value: Any) -> datetime | None:
    """Read a GitHub timestamp, always as an aware one.

    Normalising here means nothing downstream has to wonder whether a timestamp carries an
    offset. GitHub sends them, but a payload without one would otherwise be read as local time
    by whatever machine happened to be running.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
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


def team(payload: Any) -> Actor | None:
    """A GitHub team asked for a review, read as though it were a person.

    A team is not a user: it has a slug and a name where an account has a login, and no id in the
    space user ids come from. Carrying it as an Actor anyway is what lets one review request mean
    one thing all the way through, so a team is recorded, shown in the reviewers line and told
    about in the thread on exactly the same path a person is.

    What it cannot be is mentioned. `/link` binds a GitHub login to a Discord account, and a team
    has no login to bind, so a team resolves to no mention and is named in plain text. That is
    what the renderer already does for anybody nobody has linked, so it needs no special case.

    The slug is preferred over the name because it is the stable, URL-safe handle; the name is a
    display string somebody can change.
    """
    if not isinstance(payload, Mapping):
        return None
    handle = payload.get("slug") or payload.get("name")
    if not isinstance(handle, str) or not handle:
        return None
    return Actor(login=handle)


def teams(payloads: Any) -> tuple[Actor, ...]:
    if not isinstance(payloads, Iterable) or isinstance(payloads, str | bytes | Mapping):
        return ()
    parsed = (team(item) for item in payloads)
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

    # Nothing read here arrives without an owner block, so this covers one that turns up
    # malformed. Guessing the owner from full_name is recoverable; dropping the delivery is not.
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

    shared = _shared_fields(payload, repo, path="issues", action=action)
    if shared is None:
        return None

    return IssueSnapshot(**shared, closed_at=parse_timestamp(payload.get("closed_at")))


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

    shared = _shared_fields(payload, repo, path="pull", action=action)
    if shared is None:
        return None

    return PullRequestSnapshot(
        **shared,
        reviewers=actors(payload.get("requested_reviewers")),
        reviewer_teams=teams(payload.get("requested_teams")),
        # A closed pull request that was merged says so either way round, depending on which
        # endpoint or event it came from.
        merged=bool(payload.get("merged")) or payload.get("merged_at") is not None,
    )


def _shared_fields(
    payload: Payload, repo: RepositorySnapshot, *, path: str, action: str | None
) -> dict[str, Any] | None:
    """The fields every mirrored object has, or None when the payload is unusable.

    Issues and pull requests are the same shape here apart from the URL path, so pulling them
    out once is what keeps the two from validating slightly differently.
    """
    object_id = payload.get("id")
    number = payload.get("number")
    if not isinstance(object_id, int) or not isinstance(number, int):
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"{repo.html_url}/{path}/{number}"

    title = payload.get("title")
    state = payload.get("state")
    return {
        "repository": repo,
        "github_object_id": object_id,
        "number": number,
        "title": title if isinstance(title, str) else "",
        "html_url": html_url,
        "state": state if isinstance(state, str) else "open",
        "author": actor(payload.get("user")),
        "assignees": actors(payload.get("assignees")),
        "labels": labels(payload.get("labels")),
        "updated_at": parse_timestamp(payload.get("updated_at")),
        "action": action,
    }


def comment(
    payload: Any, repo: RepositorySnapshot, *, item_number: int, on: Any
) -> CommentSnapshot | None:
    """Build a snapshot from a comment object.

    `on` is the issue the comment was left under, needed only to tell which kind of item it is:
    GitHub serves pull request comments from the issues endpoint and marks them with one key.
    """
    if not isinstance(payload, Mapping):
        return None

    comment_id = payload.get("id")
    if not isinstance(comment_id, int):
        return None

    return CommentSnapshot(
        repository=repo,
        item_number=item_number,
        comment_id=comment_id,
        **_note_fields(payload, created="created_at"),
        object_type=ObjectType.PR if is_pull_request(on) else ObjectType.ISSUE,
    )


def review(payload: Any, repo: RepositorySnapshot, *, item_number: int) -> ReviewSnapshot | None:
    """Build a snapshot from a submitted review."""
    if not isinstance(payload, Mapping):
        return None

    review_id = payload.get("id")
    if not isinstance(review_id, int):
        return None

    state = payload.get("state")
    return ReviewSnapshot(
        repository=repo,
        item_number=item_number,
        review_id=review_id,
        state=state if isinstance(state, str) else "",
        **_note_fields(payload, created="submitted_at"),
    )


def _note_fields(payload: Payload, *, created: str) -> dict[str, Any]:
    """What a comment and a review carry alike.

    They differ only in which key holds the time they were written, which is why that is a
    parameter and the rest is not.
    """
    html_url = payload.get("html_url")
    body = payload.get("body")
    return {
        "html_url": html_url if isinstance(html_url, str) else "",
        "body": body if isinstance(body, str) else "",
        "author": actor(payload.get("user")),
        "created_at": parse_timestamp(payload.get(created)),
    }
