from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from shannon.discord_bot.formatting import (
    COMMENT_PREVIEW_LIMIT,
    MESSAGE_LIMIT,
    format_assignee_ping,
    format_comment,
    format_issue,
    format_review,
    thread_name,
)
from shannon.domain.enums import Priority, Status
from shannon.domain.models import (
    Actor,
    CommentSnapshot,
    IssueSnapshot,
    Label,
    RepositorySnapshot,
    ReviewSnapshot,
)

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
UPDATED = datetime(2026, 8, 11, 9, 30, tzinfo=UTC)

SNAPSHOT = IssueSnapshot(
    repository=REPO,
    github_object_id=100,
    number=12,
    title="Threads are not locked when an issue closes",
    html_url="https://github.com/Canon-Regularis/Shannon-bot/issues/12",
    state="open",
    author=Actor("octocat"),
    assignees=(Actor("hubot"),),
    labels=(Label("bug"), Label("priority: high")),
    updated_at=UPDATED,
)


def lines(message: str) -> dict[str, str]:
    result = {}
    for line in message.split("\n"):
        label, _, value = line.partition(":** ")
        result[label.removeprefix("**")] = value
    return result


def test_every_required_field_is_present() -> None:
    fields = lines(format_issue(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert list(fields) == [
        "Issue Name",
        "Type",
        "State",
        "GitHub Link",
        "Author",
        "Assignees",
        "Status",
        "Priority",
        "Tags",
        "Last Updated",
    ]


def test_there_is_no_reviewers_field() -> None:
    """GitHub issues have no reviewers, so an always-empty field would only be noise."""
    assert "Reviewers" not in format_issue(SNAPSHOT, status=Status.NOT_REVIEWED)


def test_values_come_from_the_snapshot() -> None:
    fields = lines(format_issue(SNAPSHOT, status=Status.NOT_REVIEWED, priority=Priority.HIGH))

    assert fields["Issue Name"] == "Threads are not locked when an issue closes"
    assert fields["Type"] == "Issue"
    assert fields["State"] == "Open"
    assert fields["GitHub Link"] == "https://github.com/Canon-Regularis/Shannon-bot/issues/12"
    assert fields["Author"] == "octocat"
    assert fields["Assignees"] == "hubot"
    assert fields["Priority"] == "HIGH"
    assert fields["Tags"] == "`bug`, `priority: high`"


def test_a_closed_issue_reads_closed_and_done() -> None:
    closed = replace(SNAPSHOT, state="closed")

    fields = lines(format_issue(closed, status=Status.DONE))

    assert fields["State"] == "Closed"
    assert fields["Status"] == "DONE"


def test_empty_people_and_tags_read_cleanly() -> None:
    bare = replace(SNAPSHOT, author=None, assignees=(), labels=())

    fields = lines(format_issue(bare, status=Status.NOT_REVIEWED))

    assert fields["Author"] == "None"
    assert fields["Assignees"] == "None"
    assert fields["Tags"] == "None"
    assert fields["Priority"] == "UNSET"


def test_linked_accounts_render_as_mentions() -> None:
    fields = lines(format_issue(SNAPSHOT, status=Status.NOT_REVIEWED, mentions={"hubot": 4242}))

    assert fields["Assignees"] == "<@4242>"


def test_the_thread_name_carries_the_number() -> None:
    assert thread_name(SNAPSHOT) == "#12 Threads are not locked when an issue closes"


def test_an_absurd_title_is_truncated_to_fit_discord() -> None:
    huge = replace(SNAPSHOT, title="x" * 5000)

    assert len(format_issue(huge, status=Status.NOT_REVIEWED)) <= MESSAGE_LIMIT


def test_output_is_stable_for_the_same_input() -> None:
    first = format_issue(SNAPSHOT, status=Status.NOT_REVIEWED)
    second = format_issue(SNAPSHOT, status=Status.NOT_REVIEWED)

    assert first == second


class TestAssigneePing:
    def test_it_names_the_people(self) -> None:
        assert format_assignee_ping(["hubot", "monalisa"]) == "Assigned to hubot, monalisa."

    def test_linked_people_are_mentioned(self) -> None:
        assert format_assignee_ping(["hubot"], {"hubot": 7}) == "Assigned to <@7>."

    def test_nobody_produces_nothing(self) -> None:
        assert format_assignee_ping([]) == ""


COMMENT = CommentSnapshot(
    repository=REPO,
    item_number=12,
    comment_id=999,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/issues/12#issuecomment-999",
    body="Reproduced on main.\n\nThe thread stays open.",
    author=Actor("monalisa"),
    created_at=UPDATED,
)


class TestCommentFormatting:
    def test_it_carries_everything_the_issue_asks_for(self) -> None:
        message = format_comment(COMMENT)

        assert "**monalisa** commented" in message
        assert f"<t:{int(UPDATED.timestamp())}:f>" in message
        assert "issuecomment-999" in message

    def test_the_body_is_quoted(self) -> None:
        """Quoting stops GitHub markdown restyling the thread."""
        message = format_comment(COMMENT)

        assert "> Reproduced on main." in message
        assert "> The thread stays open." in message

    def test_a_long_body_is_truncated(self) -> None:
        long_comment = replace(COMMENT, body="x" * 5000)

        message = format_comment(long_comment)

        assert len(message) <= MESSAGE_LIMIT
        assert "…" in message
        assert message.count("x") <= COMMENT_PREVIEW_LIMIT

    def test_an_empty_body_still_produces_a_message(self) -> None:
        message = format_comment(replace(COMMENT, body="   "))

        assert "**monalisa** commented" in message
        assert not any(line.startswith(">") for line in message.split("\n"))

    def test_a_linked_commenter_is_mentioned(self) -> None:
        assert "<@7>" in format_comment(COMMENT, {"monalisa": 7})

    def test_a_deleted_account_does_not_crash(self) -> None:
        message = format_comment(replace(COMMENT, author=None))

        assert "Unknown" in message

    def test_a_mass_mention_in_the_body_is_only_text(self) -> None:
        """The client suppresses these, and quoting keeps them from looking like the bot's own."""
        message = format_comment(replace(COMMENT, body="@everyone look at this"))

        assert "> @everyone look at this" in message


REVIEW = ReviewSnapshot(
    repository=REPO,
    item_number=7,
    review_id=555,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#pullrequestreview-555",
    body="Looks right, one nit inline.",
    state="approved",
    author=Actor("monalisa"),
    created_at=UPDATED,
)


class TestReviewFormatting:
    def test_an_approval_says_so(self) -> None:
        message = format_review(REVIEW)

        assert "**monalisa** approved this pull request" in message
        assert f"<t:{int(UPDATED.timestamp())}:f>" in message
        assert "pullrequestreview-555" in message

    def test_the_body_is_quoted(self) -> None:
        assert "> Looks right, one nit inline." in format_review(REVIEW)

    def test_changes_requested_reads_as_such(self) -> None:
        message = format_review(replace(REVIEW, state="changes_requested"))

        assert "**monalisa** requested changes" in message

    def test_a_plain_comment_review_reads_as_a_review(self) -> None:
        assert "left a review" in format_review(replace(REVIEW, state="commented"))

    def test_an_uppercase_state_still_resolves(self) -> None:
        """The REST API sends APPROVED, so the verdict has to be case insensitive."""
        assert "approved this pull request" in format_review(replace(REVIEW, state="APPROVED"))

    def test_an_unknown_state_falls_back_to_reviewed(self) -> None:
        assert "**monalisa** reviewed" in format_review(replace(REVIEW, state="whatever"))

    def test_an_empty_body_still_carries_the_verdict(self) -> None:
        """Approving with no comment is the common case and still worth announcing."""
        message = format_review(replace(REVIEW, body=""))

        assert "approved this pull request" in message
        assert not any(line.startswith(">") for line in message.split("\n"))

    def test_a_long_body_is_truncated(self) -> None:
        message = format_review(replace(REVIEW, body="x" * 5000))

        assert len(message) <= MESSAGE_LIMIT
        assert message.count("x") <= COMMENT_PREVIEW_LIMIT

    def test_a_linked_reviewer_is_mentioned(self) -> None:
        assert "<@7>" in format_review(REVIEW, {"monalisa": 7})

    def test_a_deleted_account_does_not_crash(self) -> None:
        assert "Unknown" in format_review(replace(REVIEW, author=None))
