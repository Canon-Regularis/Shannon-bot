from __future__ import annotations

from typing import Any

OWNER = "Canon-Regularis"
REPO = "Shannon-bot"
REPO_ID = 1255504909
PR_ID = 4661345307


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
