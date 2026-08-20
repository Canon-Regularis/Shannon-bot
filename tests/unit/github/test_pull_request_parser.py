from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shannon.github.webhooks.events import PULL_REQUEST_ACTIONS
from shannon.github.webhooks.pull_request import parse_pull_request_event
from tests.support import github_payloads as payloads

FIXTURES = Path(__file__).parents[2] / "fixtures" / "payloads"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("action", sorted(PULL_REQUEST_ACTIONS))
def test_every_supported_action_parses(action: str) -> None:
    snapshot = parse_pull_request_event(action, payloads.pull_request_event(action))

    assert snapshot is not None
    assert snapshot.action == action
    assert snapshot.number == 7


def test_a_real_github_payload_parses() -> None:
    payload = load("pull_request_opened.json")

    snapshot = parse_pull_request_event("opened", payload)

    assert snapshot is not None
    assert snapshot.repository.github_repo_id == 1255504909
    assert snapshot.repository.full_name == "Canon-Regularis/Shannon-bot"
    assert snapshot.repository.html_url == "https://github.com/Canon-Regularis/Shannon-bot"
    assert snapshot.github_object_id == 4661345307
    assert snapshot.number == 7
    assert snapshot.title == "Implement FastAPI GitHub webhook endpoint"
    assert snapshot.html_url == "https://github.com/Canon-Regularis/Shannon-bot/pull/7"
    assert snapshot.state == "open"
    assert snapshot.author is not None and snapshot.author.login == "Canon-Regularis"
    assert [a.login for a in snapshot.assignees] == ["hubot"]
    assert [r.login for r in snapshot.reviewers] == ["monalisa"]
    assert snapshot.label_names == ("backend", "high priority")
    assert snapshot.merged is False
    assert snapshot.updated_at is not None
    assert snapshot.updated_at.year == 2026


@pytest.mark.parametrize("action", ["synchronize", "ready_for_review", "milestoned", "", "opened "])
def test_unsupported_actions_are_ignored(action: str) -> None:
    assert parse_pull_request_event(action, payloads.pull_request_event("opened")) is None


@pytest.mark.parametrize("action", ["unassigned", "unlabeled"])
def test_removals_are_handled_alongside_the_additions_they_undo(action: str) -> None:
    payload = payloads.pull_request_event(action, assignees=[], labels=[])

    snapshot = parse_pull_request_event(action, payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.labels == ()


def test_a_removed_reviewer_is_not_put_straight_back() -> None:
    """`review_request_removed` names the person removed in the same field a request uses.

    Folding that in the way `review_requested` does would undo the removal.
    """
    payload = payloads.pull_request_event("review_request_removed", requested_reviewers=[])
    payload["requested_reviewer"] = payloads.user("monalisa", 200)

    snapshot = parse_pull_request_event("review_request_removed", payload)

    assert snapshot is not None
    assert snapshot.reviewers == ()


def test_a_removed_reviewer_leaves_the_others_alone() -> None:
    payload = payloads.pull_request_event(
        "review_request_removed", requested_reviewers=[payloads.user("hubot", 100)]
    )
    payload["requested_reviewer"] = payloads.user("monalisa", 200)

    snapshot = parse_pull_request_event("review_request_removed", payload)

    assert snapshot is not None
    assert [r.login for r in snapshot.reviewers] == ["hubot"]


def test_a_payload_without_a_repository_is_ignored() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["repository"]

    assert parse_pull_request_event("opened", payload) is None


def test_a_payload_without_a_pull_request_is_ignored() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["pull_request"]

    assert parse_pull_request_event("opened", payload) is None


def test_a_repository_with_no_usable_owner_block_falls_back_to_its_full_name() -> None:
    """The owner is read from `owner.login` and only reconstructed when that is unusable.

    No payload has ever arrived that way. The fallback is here because the alternative is
    dropping the delivery, and a bot that goes quiet says nothing about why.
    """
    payload = payloads.pull_request_event("opened")
    del payload["repository"]["owner"]

    snapshot = parse_pull_request_event("opened", payload)

    assert snapshot is not None
    assert snapshot.repository.owner == "Canon-Regularis"


def test_a_repository_with_neither_an_owner_nor_a_full_name_is_ignored() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["repository"]["owner"]
    payload["repository"]["full_name"] = "no-slash-here"

    assert parse_pull_request_event("opened", payload) is None


def test_a_repository_without_a_link_gets_one_built_from_its_name() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["repository"]["html_url"]

    snapshot = parse_pull_request_event("opened", payload)

    assert snapshot is not None
    assert snapshot.repository.html_url == "https://github.com/Canon-Regularis/Shannon-bot"


def test_a_pull_request_without_an_id_is_ignored() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["pull_request"]["id"]

    assert parse_pull_request_event("opened", payload) is None


def test_missing_people_and_labels_do_not_crash() -> None:
    payload = payloads.pull_request_event("edited")
    for key in ("assignees", "requested_reviewers", "labels"):
        del payload["pull_request"][key]

    snapshot = parse_pull_request_event("edited", payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.reviewers == ()
    assert snapshot.labels == ()


def test_a_deleted_author_account_does_not_crash() -> None:
    payload = payloads.pull_request_event("edited", user=None)

    snapshot = parse_pull_request_event("edited", payload)

    assert snapshot is not None
    assert snapshot.author is None


def test_nulls_where_lists_are_expected_do_not_crash() -> None:
    payload = payloads.pull_request_event("labeled", assignees=None, labels=None)

    snapshot = parse_pull_request_event("labeled", payload)

    assert snapshot is not None
    assert snapshot.assignees == ()
    assert snapshot.labels == ()


def test_labels_without_names_are_skipped() -> None:
    payload = payloads.pull_request_event(
        "labeled", labels=[{"color": "ffffff"}, {"name": "kept"}, "not-an-object"]
    )

    snapshot = parse_pull_request_event("labeled", payload)

    assert snapshot is not None
    assert snapshot.label_names == ("kept",)


def test_a_missing_timestamp_becomes_none() -> None:
    payload = payloads.pull_request_event("edited", updated_at=None)

    snapshot = parse_pull_request_event("edited", payload)

    assert snapshot is not None
    assert snapshot.updated_at is None


def test_an_unparseable_timestamp_becomes_none() -> None:
    payload = payloads.pull_request_event("edited", updated_at="last tuesday")

    snapshot = parse_pull_request_event("edited", payload)

    assert snapshot is not None
    assert snapshot.updated_at is None


def test_a_missing_html_url_is_rebuilt_from_the_repository() -> None:
    payload = payloads.pull_request_event("opened")
    del payload["pull_request"]["html_url"]

    snapshot = parse_pull_request_event("opened", payload)

    assert snapshot is not None
    assert snapshot.html_url == "https://github.com/Canon-Regularis/Shannon-bot/pull/7"


def test_review_requested_adds_the_named_reviewer() -> None:
    payload = payloads.pull_request_event("review_requested", requested_reviewers=[])
    payload["requested_reviewer"] = payloads.user("newcomer", 300)

    snapshot = parse_pull_request_event("review_requested", payload)

    assert snapshot is not None
    assert [r.login for r in snapshot.reviewers] == ["newcomer"]


def test_review_requested_does_not_duplicate_an_already_listed_reviewer() -> None:
    payload = payloads.pull_request_event("review_requested")
    payload["requested_reviewer"] = payloads.user("monalisa", 200)

    snapshot = parse_pull_request_event("review_requested", payload)

    assert snapshot is not None
    assert [r.login for r in snapshot.reviewers] == ["monalisa"]


def test_a_team_review_request_joins_the_people_already_asked() -> None:
    """This used to assert the team was dropped, which is how the gap looked deliberate.

    A review asked of a team is a review asked. It is recorded and named in the thread like
    anybody else; what it cannot be is mentioned, because /link binds a GitHub login to a Discord
    account and a team has no login to bind.
    """
    payload = payloads.pull_request_event("review_requested")
    payload["requested_team"] = {"name": "backend", "slug": "backend"}

    snapshot = parse_pull_request_event("review_requested", payload)

    assert snapshot is not None
    assert sorted(r.login for r in snapshot.reviewers) == ["backend", "monalisa"]


def test_closed_carries_the_state_through() -> None:
    payload = payloads.pull_request_event("closed", state="closed")

    snapshot = parse_pull_request_event("closed", payload)

    assert snapshot is not None
    assert snapshot.state == "closed"
    assert snapshot.merged is False
    assert snapshot.display_state == "closed"


def test_a_merged_pull_request_is_detected_from_the_flag() -> None:
    payload = payloads.pull_request_event("closed", state="closed", merged=True)

    snapshot = parse_pull_request_event("closed", payload)

    assert snapshot is not None
    assert snapshot.merged is True
    assert snapshot.display_state == "merged"


def test_a_merged_pull_request_is_detected_from_the_timestamp() -> None:
    payload = payloads.pull_request_event(
        "closed", state="closed", merged=False, merged_at="2026-08-10T13:00:00Z"
    )

    snapshot = parse_pull_request_event("closed", payload)

    assert snapshot is not None
    assert snapshot.merged is True


def test_reopened_goes_back_to_open() -> None:
    payload = payloads.pull_request_event("reopened", state="open")

    snapshot = parse_pull_request_event("reopened", payload)

    assert snapshot is not None
    assert snapshot.display_state == "open"


def test_assigned_carries_the_full_assignee_list() -> None:
    payload = payloads.pull_request_event(
        "assigned", assignees=[payloads.user("hubot", 100), payloads.user("octocat", 583231)]
    )

    snapshot = parse_pull_request_event("assigned", payload)

    assert snapshot is not None
    assert [a.login for a in snapshot.assignees] == ["hubot", "octocat"]


def test_the_snapshot_is_always_typed_as_a_pull_request() -> None:
    snapshot = parse_pull_request_event("opened", payloads.pull_request_event("opened"))

    assert snapshot is not None
    assert snapshot.object_type == "PR"


class TestAReviewAskedOfATeam:
    """GitHub can ask a team for a review, and those requests were vanishing.

    Only `requested_reviewers` was read, so a pull request whose only reviewer was a team stored
    nobody, told nobody, and showed an empty reviewers line. A team is carried as an Actor whose
    login is its slug, which is what lets one review request mean one thing the whole way through.
    """

    def test_a_requested_team_is_a_reviewer(self) -> None:
        payload = payloads.pull_request_event("opened", requested_reviewers=[])
        payload["pull_request"]["requested_teams"] = [
            {"slug": "backend-team", "name": "Backend Team", "id": 42}
        ]

        snapshot = parse_pull_request_event("opened", payload)

        assert [r.login for r in snapshot.reviewers] == ["backend-team"]

    def test_people_and_teams_are_both_carried(self) -> None:
        payload = payloads.pull_request_event("opened")
        payload["pull_request"]["requested_teams"] = [{"slug": "backend-team"}]

        snapshot = parse_pull_request_event("opened", payload)

        assert sorted(r.login for r in snapshot.reviewers) == ["backend-team", "monalisa"]

    def test_the_slug_is_preferred_over_the_display_name(self) -> None:
        """A name is a display string somebody can change; the slug is the stable handle."""
        payload = payloads.pull_request_event("opened", requested_reviewers=[])
        payload["pull_request"]["requested_teams"] = [{"slug": "core", "name": "Core Team"}]

        assert parse_pull_request_event("opened", payload).reviewers[0].login == "core"

    def test_a_team_with_only_a_name_still_counts(self) -> None:
        payload = payloads.pull_request_event("opened", requested_reviewers=[])
        payload["pull_request"]["requested_teams"] = [{"name": "Core Team"}]

        assert parse_pull_request_event("opened", payload).reviewers[0].login == "Core Team"

    def test_a_team_asked_by_the_event_itself_is_folded_in(self) -> None:
        """`review_requested` names whoever was just added at the top level, and for a team that
        is `requested_team` rather than `requested_reviewer`."""
        payload = payloads.pull_request_event("review_requested", requested_reviewers=[])
        payload["requested_team"] = {"slug": "security"}

        snapshot = parse_pull_request_event("review_requested", payload)

        assert [r.login for r in snapshot.reviewers] == ["security"]

    def test_a_team_already_on_the_list_is_not_doubled(self) -> None:
        payload = payloads.pull_request_event("review_requested", requested_reviewers=[])
        payload["pull_request"]["requested_teams"] = [{"slug": "security"}]
        payload["requested_team"] = {"slug": "security"}

        snapshot = parse_pull_request_event("review_requested", payload)

        assert [r.login for r in snapshot.reviewers] == ["security"]

    @pytest.mark.parametrize("teams", [None, "nope", [], [{}], [{"slug": ""}], [7]])
    def test_anything_that_is_not_a_team_is_ignored(self, teams: object) -> None:
        payload = payloads.pull_request_event("opened", requested_reviewers=[])
        payload["pull_request"]["requested_teams"] = teams

        assert parse_pull_request_event("opened", payload).reviewers == ()
