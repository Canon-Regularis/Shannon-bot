from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shannon.domain.enums import ObjectType, Priority
from shannon.github.webhooks.events import ISSUE_ACTIONS
from shannon.github.webhooks.issues import parse_issue_event
from tests.support import github_payloads as payloads

FIXTURES = Path(__file__).parents[2] / "fixtures" / "payloads"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("action", sorted(ISSUE_ACTIONS))
def test_every_supported_action_parses(action: str) -> None:
    snapshot = parse_issue_event(action, payloads.issue_event(action))

    assert snapshot is not None
    assert snapshot.action == action
    assert snapshot.number == 12


def test_a_real_github_payload_parses() -> None:
    snapshot = parse_issue_event("opened", load("issues_opened.json"))

    assert snapshot is not None
    assert snapshot.repository.full_name == "Canon-Regularis/Shannon-bot"
    assert snapshot.github_object_id == 4661345308
    assert snapshot.number == 12
    assert snapshot.title == "Threads are not locked when an issue closes"
    assert snapshot.html_url == "https://github.com/Canon-Regularis/Shannon-bot/issues/12"
    assert snapshot.state == "open"
    assert snapshot.author is not None and snapshot.author.login == "Canon-Regularis"
    assert [a.login for a in snapshot.assignees] == ["hubot", "monalisa"]
    assert snapshot.label_names == ("bug", "priority: high")
    assert snapshot.priority is Priority.HIGH
    assert snapshot.closed is False
    assert snapshot.updated_at is not None


def test_the_snapshot_is_always_typed_as_an_issue() -> None:
    snapshot = parse_issue_event("opened", payloads.issue_event("opened"))

    assert snapshot is not None
    assert snapshot.object_type is ObjectType.ISSUE


@pytest.mark.parametrize("action", ["milestoned", "demilestoned", "pinned", "", "opened "])
def test_unsupported_actions_are_ignored(action: str) -> None:
    assert parse_issue_event(action, payloads.issue_event("opened")) is None


@pytest.mark.parametrize("action", ["unassigned", "unlabeled"])
def test_removals_are_handled_alongside_the_additions_they_undo(action: str) -> None:
    """The list on the issue is already correct by the time the event arrives."""
    payload = payloads.issue_event(action, assignees=[], labels=[])

    snapshot = parse_issue_event(action, payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.labels == ()


def test_a_payload_without_a_repository_is_ignored() -> None:
    payload = payloads.issue_event("opened")
    del payload["repository"]

    assert parse_issue_event("opened", payload) is None


def test_a_payload_without_an_issue_is_ignored() -> None:
    payload = payloads.issue_event("opened")
    del payload["issue"]

    assert parse_issue_event("opened", payload) is None


def test_an_issue_without_an_id_is_ignored() -> None:
    payload = payloads.issue_event("opened")
    del payload["issue"]["id"]

    assert parse_issue_event("opened", payload) is None


def test_missing_people_and_labels_do_not_crash() -> None:
    payload = payloads.issue_event("edited")
    for key in ("assignees", "labels", "user"):
        del payload["issue"][key]

    snapshot = parse_issue_event("edited", payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.labels == ()
    assert snapshot.author is None
    assert snapshot.priority is Priority.UNSET


def test_nulls_where_lists_are_expected_do_not_crash() -> None:
    payload = payloads.issue_event("labeled", assignees=None, labels=None)

    snapshot = parse_issue_event("labeled", payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.labels == ()


def test_an_unparseable_timestamp_becomes_none() -> None:
    payload = payloads.issue_event("edited", updated_at="whenever")

    snapshot = parse_issue_event("edited", payload)

    assert snapshot is not None
    assert snapshot.updated_at is None


def test_a_missing_html_url_is_rebuilt_from_the_repository() -> None:
    payload = payloads.issue_event("opened")
    del payload["issue"]["html_url"]

    snapshot = parse_issue_event("opened", payload)

    assert snapshot is not None
    assert snapshot.html_url == "https://github.com/Canon-Regularis/Shannon-bot/issues/12"


def test_a_closed_issue_reports_itself_closed() -> None:
    payload = payloads.issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z")

    snapshot = parse_issue_event("closed", payload)

    assert snapshot is not None
    assert snapshot.closed is True
    assert snapshot.display_state == "closed"
    assert snapshot.closed_at is not None


def test_reopening_goes_back_to_open() -> None:
    snapshot = parse_issue_event("reopened", payloads.issue_event("reopened", state="open"))

    assert snapshot is not None
    assert snapshot.closed is False


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([{"name": "priority: high"}], Priority.HIGH),
        ([{"name": "MED_PRIORITY"}], Priority.MEDIUM),
        ([{"name": "low"}], Priority.LOW),
        ([{"name": "bug"}], Priority.UNSET),
        ([], Priority.UNSET),
    ],
)
def test_priority_comes_from_the_labels(labels: list, expected: Priority) -> None:
    snapshot = parse_issue_event("labeled", payloads.issue_event("labeled", labels=labels))

    assert snapshot is not None
    assert snapshot.priority is expected
