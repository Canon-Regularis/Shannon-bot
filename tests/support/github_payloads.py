from __future__ import annotations

from typing import Any

OWNER = "Canon-Regularis"
REPO = "Shannon-bot"
REPO_ID = 1255504909
PR_ID = 4661345307

# A pull request carries a different id on the issues endpoint than on the pulls endpoint, and
# `issue_comment` payloads use the issue one. Kept distinct here so the tests would catch a
# lookup that went through the id instead of the number.
PR_AS_ISSUE_ID = 5111095062
ISSUE_ID = 4661345308
COMMENT_ID = 2211334455
REVIEW_ID = 4846678607


def user(login: str, user_id: int = 1) -> dict[str, Any]:
    return {"login": login, "id": user_id, "type": "User"}


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


def issue(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ISSUE_ID,
        "number": 12,
        "title": "Threads are not locked when an issue closes",
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
