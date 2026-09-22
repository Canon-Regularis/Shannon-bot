from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

OWNER = "Canon-Regularis"
REPO = "Shannon-bot"
REPO_ID = 1255504909
# The commit a pull request currently points at, and so the one a check suite reports on. Its
# own constant rather than either push SHA, because those two say where the branch moved FROM
# and TO and this says where it IS.
CHECKED_SHA = "c" * 40
CHECK_SUITE_ID = 95896524045
PR_ID = 4661345307

# A pull request carries a different id on the issues endpoint than on the pulls endpoint, and
# `issue_comment` payloads use the issue one. Kept distinct here so the tests would catch a
# lookup that went through the id instead of the number.
PR_AS_ISSUE_ID = 5111095062
ISSUE_ID = 4661345308
COMMENT_ID = 2211334455
REVIEW_ID = 4846678607
# Deliberately next door to COMMENT_ID. The two live in separate key spaces, and a test that
# quietly mixed them would be easier to believe if the numbers looked nothing alike.
REVIEW_COMMENT_ID = 2211334456


def user(login: str, user_id: int = 1) -> dict[str, Any]:
    """An account, the way GitHub sends one.

    `avatar_url` is carried because every real user object has one and issue #116 reads it for the
    thumbnail on a panel. Without it here the refresh and board-poll paths, which build snapshots
    from REST through the same mapping, would quietly have no pictures and no test would notice.
    """
    return {
        "login": login,
        "id": user_id,
        "type": "User",
        "avatar_url": f"https://avatars.githubusercontent.com/u/{user_id}?v=4",
    }


def repository(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": REPO_ID,
        "name": REPO,
        "full_name": f"{OWNER}/{REPO}",
        "html_url": f"https://github.com/{OWNER}/{REPO}",
        "owner": user(OWNER, 80922799),
        "private": False,
    }
    payload.update(overrides)
    return payload


def pull_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": PR_ID,
        "number": 7,
        "title": "Add the webhook endpoint",
        "body": "Answers the signature check and writes the delivery down.",
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/7",
        "state": "open",
        "merged": False,
        "merged_at": None,
        "user": user("octocat", 583231),
        "assignees": [user("hubot", 100)],
        "requested_reviewers": [user("monalisa", 200)],
        "labels": [{"name": "backend", "color": "0e8a16"}],
        "updated_at": "2026-08-10T12:00:00Z",
        "base": {"ref": "main", "repo": repository()},
        # Both carried because GitHub sends both and issue #112 reads both: the head to tell a CI
        # result about this commit from one about a commit the branch has moved off, and the draft
        # flag to decide whether reviewers are rung at all.
        "head": {"ref": "feature", "sha": CHECKED_SHA},
        "draft": False,
    }
    payload.update(overrides)
    return payload


def pull_request_event(action: str = "opened", **pr_overrides: Any) -> dict[str, Any]:
    """A webhook body shaped the way GitHub sends it."""
    return {
        "action": action,
        "number": pr_overrides.get("number", 7),
        "pull_request": pull_request(**pr_overrides),
        "repository": repository(),
        "sender": user("octocat", 583231),
    }


# The two ends of a push. Distinct strings rather than realistic hashes, so a test asserting on
# which compare was asked for reads as the pair it named.
BEFORE_PUSH = "1" * 40
AFTER_PUSH = "2" * 40


def push_event(
    *, before: str = BEFORE_PUSH, after: str = AFTER_PUSH, pusher: str = "octocat", **overrides: Any
) -> dict[str, Any]:
    """A `pull_request.synchronize` body: somebody pushed to the branch of an open pull request.

    The two SHAs sit at the top level beside the action, which is the whole of what GitHub says
    about a push here. What actually landed takes a call to find out.
    """
    payload = pull_request_event("synchronize", **overrides)
    payload["before"] = before
    payload["after"] = after
    payload["sender"] = user(pusher, 583231)
    return payload


def issue(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ISSUE_ID,
        "number": 12,
        "title": "Threads are not locked when an issue closes",
        "body": "Closing an issue leaves its thread open to replies.",
        "html_url": f"https://github.com/{OWNER}/{REPO}/issues/12",
        "state": "open",
        "state_reason": None,
        "user": user("octocat", 583231),
        "assignees": [user("hubot", 100)],
        "labels": [{"name": "bug", "color": "d73a4a"}],
        "comments": 0,
        "created_at": "2026-08-11T09:00:00Z",
        "updated_at": "2026-08-11T09:30:00Z",
        "closed_at": None,
    }
    payload.update(overrides)
    return payload


def issue_event(action: str = "opened", **issue_overrides: Any) -> dict[str, Any]:
    return {
        "action": action,
        "issue": issue(**issue_overrides),
        "repository": repository(),
        "sender": user("octocat", 583231),
    }


def comment(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": COMMENT_ID,
        "html_url": f"https://github.com/{OWNER}/{REPO}/issues/12#issuecomment-{COMMENT_ID}",
        "user": user("monalisa", 200),
        "body": "Reproduced on main, the thread stays open after closing.",
        "created_at": "2026-08-11T10:00:00Z",
        "updated_at": "2026-08-11T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def issue_comment_event(
    action: str = "created", *, on: dict[str, Any] | None = None, **comment_overrides: Any
) -> dict[str, Any]:
    """A comment event. `on` is the issue or pull-request-as-issue it was left on.

    GitHub sends this event for pull requests too, with the pull request represented as an
    issue, which is why the item is a parameter rather than always an issue.
    """
    return {
        "action": action,
        "issue": on if on is not None else issue(),
        "comment": comment(**comment_overrides),
        "repository": repository(),
        "sender": user("monalisa", 200),
    }


def pull_request_as_issue(**overrides: Any) -> dict[str, Any]:
    """How a pull request looks inside an `issue_comment` payload.

    The id is the issue id, which is not the pull request id stored against the tracked item.
    That difference is why comments are matched on number.
    """
    payload = issue(
        id=PR_AS_ISSUE_ID,
        number=7,
        title="Add the webhook endpoint",
        html_url=f"https://github.com/{OWNER}/{REPO}/pull/7",
        pull_request={"url": f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/7"},
    )
    payload.update(overrides)
    return payload


def review(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": REVIEW_ID,
        "node_id": "PRR_kwDOStV8Dc8AAAAB",
        "user": user("monalisa", 200),
        "body": "Looks right, one nit inline.",
        # Webhooks send this lowercased; the REST API sends it uppercase.
        "state": "approved",
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/7#pullrequestreview-{REVIEW_ID}",
        "pull_request_url": f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/7",
        "commit_id": "6dcb09b5b57875f334f61aebed695e2e4193db5e",
        "submitted_at": "2026-08-11T11:00:00Z",
        "author_association": "MEMBER",
    }
    payload.update(overrides)
    return payload


def pull_request_review_event(action: str = "submitted", **review_overrides: Any) -> dict[str, Any]:
    return {
        "action": action,
        "review": review(**review_overrides),
        "pull_request": pull_request(),
        "repository": repository(),
        "sender": user("monalisa", 200),
    }


def review_comment(**overrides: Any) -> dict[str, Any]:
    """One inline comment on a diff, as a single-line comment, which is what most of them are.

    There is no `in_reply_to_id` key at all rather than one holding None. Absence is what GitHub
    sends on a comment that opens a thread, and absence is what the parser has to survive.
    """
    payload: dict[str, Any] = {
        "id": REVIEW_COMMENT_ID,
        "pull_request_review_id": REVIEW_ID,
        "path": "shannon/services/notes.py",
        "diff_hunk": "@@ -200,6 +200,9 @@ class ItemNoteMirror:",
        "commit_id": "6dcb09b5b57875f334f61aebed695e2e4193db5e",
        "original_commit_id": "9c2a1f0bb6a5c34c2cbb0f5ddc0e8a5a8b3d1e77",
        # Deprecated by GitHub and read by nothing here. Carried so the fixture looks like the
        # body that actually arrives, rather than like the subset this bot happens to want.
        "position": 14,
        "original_position": 14,
        "line": 205,
        "original_line": 205,
        "start_line": None,
        "original_start_line": None,
        "side": "RIGHT",
        "start_side": None,
        "subject_type": "line",
        "user": user("monalisa", 200),
        "body": "This claim wants giving back on cancellation too.",
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/7#discussion_r{REVIEW_COMMENT_ID}",
        "created_at": "2026-08-11T10:30:00Z",
        "updated_at": "2026-08-11T10:30:00Z",
    }
    payload.update(overrides)
    return payload


def pull_request_review_comment_event(
    action: str = "created", **comment_overrides: Any
) -> dict[str, Any]:
    return {
        "action": action,
        "comment": review_comment(**comment_overrides),
        "pull_request": pull_request(),
        "repository": repository(),
        "sender": user("monalisa", 200),
    }


def check_run(**overrides: Any) -> dict[str, Any]:
    """One job inside a check suite, shaped the way the check-runs endpoint sends it."""
    payload: dict[str, Any] = {
        "id": 105762136252,
        "name": "Lint, format and types",
        "status": "completed",
        "conclusion": "success",
        "html_url": (
            f"https://github.com/{OWNER}/{REPO}/actions/runs/35395138860/job/105762136252"
        ),
    }
    payload.update(overrides)
    return payload


def check_runs_page(*runs: dict[str, Any]) -> dict[str, Any]:
    """A page of the check-runs endpoint, which answers an OBJECT rather than an array.

    Kept here rather than built inline in each test, because getting that wrapper wrong is the
    mistake the reader was written to avoid and a fixture that shared the mistake would hide it.
    """
    return {"total_count": len(runs), "check_runs": list(runs)}


def check_suite_event(
    action: str = "completed",
    *,
    head_sha: str = CHECKED_SHA,
    numbers: Sequence[int] = (7,),
    **overrides: Any,
) -> dict[str, Any]:
    """A `check_suite` body: CI has finished with a commit.

    `pull_requests` is what ties it to a thread, and it is the only thing that can: nothing in
    this project turns a bare SHA into a tracked item. GitHub leaves it empty for a fork and for a
    commit on the default branch, which `numbers=()` is how a test says.
    """
    suite: dict[str, Any] = {
        "id": CHECK_SUITE_ID,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "pull_requests": [{"number": number} for number in numbers],
    }
    suite.update(overrides)
    return {
        "action": action,
        "check_suite": suite,
        "repository": repository(),
        "sender": user("octocat", 583231),
    }


FIXTURES = Path(__file__).parents[1] / "fixtures" / "payloads"


def load(name: str) -> dict[str, Any]:
    """A recorded GitHub webhook body, read off disk."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
