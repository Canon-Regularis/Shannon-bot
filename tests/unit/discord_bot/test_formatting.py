from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from shannon.discord_bot.formatting import MESSAGE_LIMIT, format_pull_request
from shannon.domain.enums import Priority, Status
from shannon.domain.models import Actor, Label, PullRequestSnapshot, RepositorySnapshot

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
UPDATED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

SNAPSHOT = PullRequestSnapshot(
    repository=REPO,
    github_object_id=100,
    number=7,
    title="Add the webhook endpoint",
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
    state="open",
    author=Actor("octocat"),
    assignees=(Actor("hubot"),),
    reviewers=(Actor("monalisa"),),
    labels=(Label("backend"), Label("bug")),
    updated_at=UPDATED,
)


def lines(message: str) -> dict[str, str]:
    result = {}
    for line in message.split("\n"):
        label, _, value = line.partition(":** ")
        result[label.removeprefix("**")] = value
    return result


def test_every_required_field_is_present() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert list(fields) == [
        "PR Name",
        "Type",
        "State",
        "GitHub Link",
        "Author",
        "Assignees",
        "Reviewers",
        "Status",
        "Priority",
        "Tags",
        "Last Updated",
    ]


def test_values_come_from_the_snapshot() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert fields["PR Name"] == "Add the webhook endpoint"
    assert fields["Type"] == "PR"
    assert fields["GitHub Link"] == "https://github.com/Canon-Regularis/Shannon-bot/pull/7"
    assert fields["Author"] == "octocat"
    assert fields["Assignees"] == "hubot"
    assert fields["Reviewers"] == "monalisa"


def test_new_pull_requests_show_not_reviewed_and_unset() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert fields["Status"] == "NOT_REVIEWED"
    assert fields["Priority"] == "UNSET"


def test_labels_are_listed_under_tags() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert fields["Tags"] == "`backend`, `bug`"


def test_empty_people_and_tags_read_cleanly() -> None:
    bare = replace(SNAPSHOT, author=None, assignees=(), reviewers=(), labels=())

    fields = lines(format_pull_request(bare, status=Status.NOT_REVIEWED))

    assert fields["Author"] == "None"
    assert fields["Assignees"] == "None"
    assert fields["Reviewers"] == "None"
    assert fields["Tags"] == "None"


def test_multiple_people_are_comma_separated() -> None:
    many = replace(SNAPSHOT, assignees=(Actor("hubot"), Actor("octocat"), Actor("monalisa")))

    assert lines(format_pull_request(many, status=Status.NOT_REVIEWED))["Assignees"] == (
        "hubot, octocat, monalisa"
    )


def test_linked_accounts_render_as_discord_mentions() -> None:
    fields = lines(
        format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED, mentions={"monalisa": 424242})
    )

    assert fields["Reviewers"] == "<@424242>"


def test_unlinked_accounts_stay_plain_usernames() -> None:
    fields = lines(
        format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED, mentions={"someone-else": 424242})
    )

    assert fields["Reviewers"] == "monalisa"


def test_mention_lookup_ignores_login_case() -> None:
    upper = replace(SNAPSHOT, reviewers=(Actor("MonaLisa"),))

    fields = lines(format_pull_request(upper, status=Status.NOT_REVIEWED, mentions={"monalisa": 7}))

    assert fields["Reviewers"] == "<@7>"


def test_timestamp_uses_discord_markup() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert fields["Last Updated"] == f"<t:{int(UPDATED.timestamp())}:f>"


def test_missing_timestamp_reads_unknown() -> None:
    fields = lines(format_pull_request(replace(SNAPSHOT, updated_at=None), status=Status.DONE))

    assert fields["Last Updated"] == "Unknown"


def test_status_and_priority_are_taken_from_the_caller() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.IN_REVIEW, priority=Priority.HIGH))

    assert fields["Status"] == "IN_REVIEW"
    assert fields["Priority"] == "HIGH"


def test_output_is_stable_for_the_same_input() -> None:
    first = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED)
    second = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED)

    assert first == second


def test_an_absurd_title_is_truncated_to_fit_discord() -> None:
    huge = replace(SNAPSHOT, title="x" * 5000)

    message = format_pull_request(huge, status=Status.NOT_REVIEWED)

    assert len(message) <= MESSAGE_LIMIT


def test_a_missing_title_reads_unknown() -> None:
    fields = lines(format_pull_request(replace(SNAPSHOT, title=""), status=Status.NOT_REVIEWED))

    assert fields["PR Name"] == "Unknown"
