from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from shannon.domain.enums import ObjectType
from shannon.domain.json import JsonObject, is_json_list, is_json_object
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
    created_at: datetime | None


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
    return _each(payloads, actor)


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
    return _each(payloads, team)


def labels(payloads: object) -> tuple[Label, ...]:
    if not is_json_list(payloads):
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

    found = _owner(payload)
    if found is None:
        return None
    owner, owner_id = found

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
        owner_id=owner_id,
    )


def _owner(payload: JsonObject) -> tuple[str, int | None] | None:
    """The owner's login, and its account id where the owner block carries one."""
    owner = actor(payload.get("owner"))
    if owner is not None:
        return owner.login, owner.github_user_id

    # Nothing read here arrives without an owner block; this covers one that turns up malformed,
    # which names an account and says nothing about which one it is.
    full_name = payload.get("full_name")
    if isinstance(full_name, str) and "/" in full_name:
        return full_name.split("/", 1)[0], None
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


def _head_sha(payload: JsonObject) -> str:
    """The issues shape of a pull request carries no head, so this can come back empty."""
    head = payload.get("head")
    return _str_or_empty(head.get("sha") if is_json_object(head) else None)


def _shared_fields(
    payload: JsonObject, repo: RepositorySnapshot, *, path: str, action: str | None
) -> _SharedFields | None:
    object_id = payload.get("id")
    number = payload.get("number")
    if not isinstance(object_id, int) or not isinstance(number, int):
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url:
        html_url = f"{repo.html_url}/{path}/{number}"

    state = payload.get("state")
    return {
        "repository": repo,
        "github_object_id": object_id,
        "number": number,
        "title": _str_or_empty(payload.get("title")),
        "html_url": html_url,
        "state": state if isinstance(state, str) else "open",
        "author": actor(payload.get("user")),
        "assignees": actors(payload.get("assignees")),
        "labels": labels(payload.get("labels")),
        "updated_at": parse_timestamp(payload.get("updated_at")),
        "action": action,
        # GitHub sends a null body for an item opened with no description.
        "body": _str_or_empty(payload.get("body")),
        # Issue #259: the names a description writes ring only while the item is new.
        "created_at": parse_timestamp(payload.get("created_at")),
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

    return ReviewSnapshot(
        repository=repo,
        item_number=item_number,
        review_id=review_id,
        state=_str_or_empty(payload.get("state")),
        # Issue #263 reads a file's history from the commit the review was made on. The webhook
        # and the REST list both carry it, so both readers get it from here.
        commit_id=_str_or_empty(payload.get("commit_id")),
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

    return ReviewCommentSnapshot(
        repository=repo,
        item_number=item_number,
        comment_id=comment_id,
        path=_str_or_empty(payload.get("path")),
        line=_optional_int(payload.get("line")),
        start_line=_optional_int(payload.get("start_line")),
        original_line=_optional_int(payload.get("original_line")),
        in_reply_to_id=_optional_int(payload.get("in_reply_to_id")),
        # Which review the comment came in with. Issue #263 picks one review's files out of the
        # whole list by it, because GitHub's list of a pull request's comments mixes every review.
        review_id=_optional_int(payload.get("pull_request_review_id")),
        **_note_fields(payload, created="created_at"),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _str_or_empty(value: object) -> str:
    """A text field, or empty where GitHub sent none.

    The partner of `_optional_int`, for the fields a snapshot carries as a string either way.
    Not called `_text`, because `projects._text` already means a string or None.
    """
    return value if isinstance(value, str) else ""


def _each[T](rows: object, read: Callable[[object], T | None]) -> tuple[T, ...]:
    """Every row of a list `read` can make sense of; one it cannot is dropped, not the list."""
    if not is_json_list(rows):
        return ()
    parsed = (read(row) for row in rows)
    return tuple(found for found in parsed if found is not None)


def _note_fields(payload: JsonObject, *, created: str) -> _NoteFields:
    return {
        "html_url": _str_or_empty(payload.get("html_url")),
        "body": _str_or_empty(payload.get("body")),
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
    parents = payload.get("parents")
    return CommitRef(
        sha=sha,
        message=_str_or_empty(inner.get("message") if is_json_object(inner) else None),
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

    commits = _each(payload.get("commits"), commit_ref)
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

    return CheckRun(
        check_run_id=check_run_id,
        name=name,
        # Empty where GitHub did not say, which reads downstream as "not completed" and holds
        # the announcement back.
        status=_str_or_empty(payload.get("status")),
        conclusion=_str_or_empty(payload.get("conclusion")),
        html_url=_str_or_empty(payload.get("html_url")),
    )


def check_runs(payload: object) -> list[CheckRun]:
    """The usable runs on one page of the check-runs endpoint.

    That endpoint answers an object with the list under `check_runs`, unlike the labels list in
    the client, which pages through an array directly.
    """
    if not is_json_object(payload):
        return []
    return list(_each(payload.get("check_runs"), check_run))
