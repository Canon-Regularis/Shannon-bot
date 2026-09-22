from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from shannon.domain.enums import ObjectType
from shannon.domain.json import JsonObject, is_json_array, is_json_list, is_json_object
from shannon.domain.models import (
    Actor,
    CheckRun,
    CommentSnapshot,
    CommitRange,
    CommitRef,
    CommitStats,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewCommentSnapshot,
    ReviewSnapshot,
)
from shannon.domain.time import as_utc

Payload = JsonObject


class _SharedFields(TypedDict):
    """Typed so the `**` into a snapshot stays checked.

    `dict[str, object]` will not unpack into a typed constructor at all, and `dict[str, Any]`
    unpacks into anything.
    """

    repository: RepositorySnapshot
    github_object_id: int
    number: int
    title: str
    html_url: str
    state: str
    author: Actor | None
    assignees: tuple[Actor, ...]
    labels: tuple[Label, ...]
    updated_at: datetime | None
    action: str | None
    body: str


class _NoteFields(TypedDict):
    html_url: str
    body: str
    author: Actor | None
    created_at: datetime | None


def parse_timestamp(value: object) -> datetime | None:
    """Read a GitHub timestamp as aware; one with no offset would be read as local time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def actor(payload: object) -> Actor | None:
    """GitHub sends `null` for a deleted account, so this has to tolerate a missing object."""
    if not is_json_object(payload):
        return None
    login = payload.get("login")
    if not isinstance(login, str) or not login:
        return None
    github_user_id = payload.get("id")
    return Actor(
        login=login,
        github_user_id=github_user_id if isinstance(github_user_id, int) else None,
        avatar_url=_avatar(payload.get("avatar_url")),
    )


def _avatar(value: object) -> str | None:
    """The account's picture, or None for anything but an `https://` URL.

    Discord fetches a thumbnail's media itself and refuses the whole message when it cannot.
    """
    return value if isinstance(value, str) and value.startswith("https://") else None


def actors(payloads: object) -> tuple[Actor, ...]:
    if not is_json_array(payloads):
        return ()
    parsed = (actor(item) for item in payloads)
    return tuple(item for item in parsed if item is not None)


def team(payload: object) -> Actor | None:
    """A GitHub team asked for a review, carried as an Actor.

    A team has no login, so `/link` cannot bind it and it renders as plain text the way an
    unlinked person does. The slug is the stable handle; the name is one somebody can change.
    """
    if not is_json_object(payload):
        return None
    handle = payload.get("slug") or payload.get("name")
    if not isinstance(handle, str) or not handle:
        return None
    return Actor(login=handle)


def teams(payloads: object) -> tuple[Actor, ...]:
    if not is_json_array(payloads):
        return ()
    parsed = (team(item) for item in payloads)
    return tuple(item for item in parsed if item is not None)


def labels(payloads: object) -> tuple[Label, ...]:
    if not is_json_array(payloads):
        return ()

    result: list[Label] = []
    for item in payloads:
        if not is_json_object(item):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            colour = item.get("color")
            result.append(Label(name=name, color=colour if isinstance(colour, str) else None))
    return tuple(result)


def repository(payload: object) -> RepositorySnapshot | None:
    if not is_json_object(payload):
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

    # Checked for the type, not coerced: `bool(...)` reads a missing field, a null and the string
    # "false" all as public, and the column behind this keeps "nobody said" apart from "no".
    private = payload.get("private")
    return RepositorySnapshot(
        github_repo_id=repo_id,
        owner=owner,
        name=name,
        html_url=html_url,
        private=private if isinstance(private, bool) else None,
    )


def _owner_login(payload: Payload) -> str | None:
    owner = actor(payload.get("owner"))
    if owner is not None:
        return owner.login

    # Nothing read here arrives without an owner block; this covers one that turns up malformed.
    full_name = payload.get("full_name")
    if isinstance(full_name, str) and "/" in full_name:
        return full_name.split("/", 1)[0]
    return None


def issue(
    payload: object, repo: RepositorySnapshot, *, action: str | None = None
) -> IssueSnapshot | None:
    """Build a snapshot from an issue object.

    The same shape comes back from `GET /repos/{owner}/{repo}/issues/{number}`, from a row of
    `GET /repos/{owner}/{repo}/issues`, and inside `issues` webhook payloads.
    """
    if not is_json_object(payload):
        return None

    shared = _shared_fields(payload, repo, path="issues", action=action)
    if shared is None:
        return None

    return IssueSnapshot(**shared, closed_at=parse_timestamp(payload.get("closed_at")))


def is_pull_request(payload: object) -> bool:
    """Whether an issue-shaped payload is really a pull request.

    GitHub serves pull requests from the issues endpoint too, and marks them only with this key.
    Without it, `/issue` on a pull request number would track it again under the wrong type.
    """
    return is_json_object(payload) and payload.get("pull_request") is not None


def pull_request(
    payload: object, repo: RepositorySnapshot, *, action: str | None = None
) -> PullRequestSnapshot | None:
    """Build a snapshot from a pull request object.

    Read only from `GET /repos/{owner}/{repo}/pulls/...` or a `pull_request` webhook: the *issues*
    shape of a pull request has no requested reviewers, and this reads one as nobody being asked.
    """
    if not is_json_object(payload):
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
        head_sha=_head_sha(payload),
        draft=payload.get("draft") is True,
    )


def _head_sha(payload: Payload) -> str:
    """The issues shape of a pull request carries no head, so this can come back empty."""
    head = payload.get("head")
    sha = head.get("sha") if is_json_object(head) else None
    return sha if isinstance(sha, str) else ""


def _shared_fields(
    payload: Payload, repo: RepositorySnapshot, *, path: str, action: str | None
) -> _SharedFields | None:
    object_id = payload.get("id")
    number = payload.get("number")
    if not isinstance(object_id, int) or not isinstance(number, int):
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"{repo.html_url}/{path}/{number}"

    title = payload.get("title")
    state = payload.get("state")
    # GitHub sends a null body for an item opened with no description.
    body = payload.get("body")
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
        "body": body if isinstance(body, str) else "",
    }


def comment(
    payload: object, repo: RepositorySnapshot, *, item_number: int, on: object
) -> CommentSnapshot | None:
    """Build a snapshot from a comment object; `on` is the issue it was left under."""
    if not is_json_object(payload):
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


def review(payload: object, repo: RepositorySnapshot, *, item_number: int) -> ReviewSnapshot | None:
    if not is_json_object(payload):
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


def review_comment(
    payload: object, repo: RepositorySnapshot, *, item_number: int
) -> ReviewCommentSnapshot | None:
    """Build a snapshot from one inline comment on a pull request's diff.

    GitHub leaves `start_line` out of a single-line comment, leaves `in_reply_to_id` out of one
    that opens a thread, and empties `line` on one the diff has moved out from under.
    """
    if not is_json_object(payload):
        return None

    comment_id = payload.get("id")
    if not isinstance(comment_id, int):
        return None

    path = payload.get("path")
    return ReviewCommentSnapshot(
        repository=repo,
        item_number=item_number,
        comment_id=comment_id,
        path=path if isinstance(path, str) else "",
        line=_optional_int(payload.get("line")),
        start_line=_optional_int(payload.get("start_line")),
        original_line=_optional_int(payload.get("original_line")),
        in_reply_to_id=_optional_int(payload.get("in_reply_to_id")),
        **_note_fields(payload, created="created_at"),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _note_fields(payload: Payload, *, created: str) -> _NoteFields:
    html_url = payload.get("html_url")
    body = payload.get("body")
    return {
        "html_url": html_url if isinstance(html_url, str) else "",
        "body": body if isinstance(body, str) else "",
        "author": actor(payload.get("user")),
        "created_at": parse_timestamp(payload.get(created)),
    }


def commit_ref(payload: object) -> CommitRef | None:
    """One row of a compare; a row with no SHA is dropped rather than rendered as a gap."""
    if not is_json_object(payload):
        return None
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        return None

    inner = payload.get("commit")
    message = inner.get("message") if is_json_object(inner) else None
    parents = payload.get("parents")
    return CommitRef(
        sha=sha,
        message=message if isinstance(message, str) else "",
        # `author` and not `commit.author`. The first is the GitHub account, resolved from the
        # email address; the second is whatever the committer typed into their git config.
        author=actor(payload.get("author")),
        merge=is_json_list(parents) and len(parents) > 1,
    )


def commit_range(payload: object) -> CommitRange | None:
    """A compare between two commits; one unusable row does not lose the rest."""
    if not is_json_object(payload):
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None

    rows = payload.get("commits")
    rows = rows if is_json_list(rows) else []
    parsed = (commit_ref(row) for row in rows)
    commits = tuple(found for found in parsed if found is not None)
    total = payload.get("total_commits")
    return CommitRange(
        status=status,
        commits=commits,
        # Not zero: a missing count with commits beside it would report every one as left out.
        total=total if isinstance(total, int) else len(commits),
    )


def commit_stats(payload: object) -> CommitStats | None:
    """How much one commit changed, or None for a body without the numbers.

    GitHub sends no file count on a commit, and the list it sends instead stops at three hundred
    entries, so a very wide commit understates its files while the other two stay exact.
    """
    if not is_json_object(payload):
        return None
    stats = payload.get("stats")
    if not is_json_object(stats):
        return None

    additions = stats.get("additions")
    deletions = stats.get("deletions")
    if not isinstance(additions, int) or not isinstance(deletions, int):
        return None

    files = payload.get("files")
    return CommitStats(
        additions=additions,
        deletions=deletions,
        changed_files=len(files) if is_json_list(files) else 0,
    )


def check_run(payload: object) -> CheckRun | None:
    """One CI job off the check-runs endpoint, or None for a row that cannot be rendered.

    GitHub leaves the conclusion null on a run it has not finished with, and the caller refuses
    the whole set on that before it reaches here.
    """
    if not is_json_object(payload):
        return None

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return None

    check_run_id = payload.get("id")
    if not isinstance(check_run_id, int):
        return None

    status = payload.get("status")
    conclusion = payload.get("conclusion")
    html_url = payload.get("html_url")
    return CheckRun(
        check_run_id=check_run_id,
        name=name,
        # Empty where GitHub did not say, which reads downstream as "not completed" and holds
        # the announcement back.
        status=status if isinstance(status, str) else "",
        conclusion=conclusion if isinstance(conclusion, str) else "",
        html_url=html_url if isinstance(html_url, str) else "",
    )


def check_runs(payload: object) -> list[CheckRun]:
    """The usable runs on one page of the check-runs endpoint.

    That endpoint answers an object with the list under `check_runs`, unlike the labels list in
    the client, which pages through an array directly.
    """
    if not is_json_object(payload):
        return []
    rows = payload.get("check_runs")
    if not is_json_list(rows):
        return []
    return [found for found in (check_run(row) for row in rows) if found is not None]
