from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from shannon.discord_bot.formatting import (
    format_assignee_ping,
    format_comment,
    format_issue,
    format_review,
    format_team_ping,
    thread_name,
)
from shannon.discord_bot.panels import PANEL_BUDGET, BlockKind, Panel
from shannon.discord_bot.safe_text import COMMENT_PREVIEW_LIMIT
from shannon.domain.enums import ObjectType, Priority, Status
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


def lines(block: Panel) -> dict[str, str]:
    """The field rows, read out of the one block that holds them.

    That block rather than the whole card, which is what this used to split. The description
    sits in a block of its own since issue #113, so its own lines can no longer arrive here
    looking like rows whose label happened to be missing.
    """
    result = {}
    (fields,) = [part.text for part in block.blocks if part.kind is BlockKind.FIELDS]
    for line in fields.split("\n"):
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
    assert "Reviewers" not in format_issue(SNAPSHOT, status=Status.NOT_REVIEWED).text


def test_values_come_from_the_snapshot() -> None:
    fields = lines(format_issue(SNAPSHOT, status=Status.NOT_REVIEWED, priority=Priority.HIGH))

    assert fields["Issue Name"] == "Threads are not locked when an issue closes"
    assert fields["Type"] == "Issue"
    assert fields["State"] == "Open"
    assert fields["GitHub Link"] == "https://github.com/Canon-Regularis/Shannon-bot/issues/12"
    assert fields["Author"] == "octocat"
    assert fields["Assignees"] == "hubot"
    assert fields["Priority"] == "High"
    assert fields["Tags"] == "`bug`, `priority: high`"


def test_a_closed_issue_reads_closed_and_done() -> None:
    closed = replace(SNAPSHOT, state="closed")

    fields = lines(format_issue(closed, status=Status.DONE))

    assert fields["State"] == "Closed"
    assert fields["Status"] == "Done"


def test_empty_people_and_tags_read_cleanly() -> None:
    bare = replace(SNAPSHOT, author=None, assignees=(), labels=())

    fields = lines(format_issue(bare, status=Status.NOT_REVIEWED))

    assert fields["Author"] == "None"
    assert fields["Assignees"] == "None"
    assert fields["Tags"] == "None"
    assert fields["Priority"] == "None"


def test_linked_accounts_render_as_mentions() -> None:
    fields = lines(format_issue(SNAPSHOT, status=Status.NOT_REVIEWED, mentions={"hubot": 4242}))

    assert fields["Assignees"] == "<@4242>"


def test_the_thread_name_carries_the_number() -> None:
    assert thread_name(SNAPSHOT) == "#12 Threads are not locked when an issue closes"


def test_an_absurd_title_is_truncated_to_fit_discord() -> None:
    huge = replace(SNAPSHOT, title="x" * 5000)

    assert format_issue(huge, status=Status.NOT_REVIEWED).trimmed().length() <= PANEL_BUDGET


def test_output_is_stable_for_the_same_input() -> None:
    first = format_issue(SNAPSHOT, status=Status.NOT_REVIEWED)
    second = format_issue(SNAPSHOT, status=Status.NOT_REVIEWED)

    assert first == second


class TestAssigneePing:
    def test_it_names_the_people(self) -> None:
        assert format_assignee_ping(["hubot", "monalisa"]).text == "Assigned to hubot, monalisa."

    def test_linked_people_are_mentioned(self) -> None:
        assert format_assignee_ping(["hubot"], {"hubot": 7}).text == "Assigned to <@7>."

    def test_nobody_produces_a_card_with_nothing_in_it(self) -> None:
        """Which is the same answer in the new vocabulary: the caller asks whether there are
        blocks exactly where it used to ask whether the string was empty."""
        assert format_assignee_ping([]).blocks == ()


class TestTeamPing:
    """A team is written with its own syntax, and `<@123>` for a role id resolves to nobody."""

    def test_a_linked_team_is_a_role_mention(self) -> None:
        assert format_team_ping(["backend"], {"backend": 900}).text == (
            "Review requested from <@&900>."
        )

    def test_an_unlinked_team_is_still_named(self) -> None:
        """The same bargain every renderer here makes: the thread records who GitHub asked for
        even where nobody has run /link_team for them."""
        assert format_team_ping(["backend"]).text == "Review requested from backend."

    def test_nobody_produces_a_card_with_nothing_in_it(self) -> None:
        """Previously written as a conditional expression, which coverage does not branch on, so
        this case went untested for as long as it existed. The caller reads the blocks: a panel
        with none is a message Discord would refuse, and posting one is what this stops.
        """
        assert format_team_ping([]).blocks == ()


COMMENT = CommentSnapshot(
    repository=REPO,
    item_number=12,
    comment_id=999,
    object_type=ObjectType.ISSUE,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/issues/12#issuecomment-999",
    body="Reproduced on main.\n\nThe thread stays open.",
    author=Actor("monalisa"),
    created_at=UPDATED,
)


class TestCommentFormatting:
    def test_it_carries_everything_the_issue_asks_for(self) -> None:
        message = format_comment(COMMENT).text

        assert "**monalisa** commented" in message
        assert f"<t:{int(UPDATED.timestamp())}:f>" in message
        assert "issuecomment-999" in message

    def test_the_body_is_a_block_of_its_own(self) -> None:
        """Unquoted since issue #113. What stops GitHub markdown restyling the thread is the
        escaping, which is where it always was; the markers were only ever decoration."""
        body = format_comment(COMMENT).blocks[1]

        assert body.kind is BlockKind.BODY
        assert body.text == "Reproduced on main.\n\nThe thread stays open."

    def test_a_long_body_is_truncated(self) -> None:
        long_comment = replace(COMMENT, body="x" * 5000)

        message = format_comment(long_comment).text

        assert "…" in message
        assert message.count("x") == COMMENT_PREVIEW_LIMIT, (
            "the comment limit is what cuts a comment, not the description one"
        )

    def test_an_empty_body_leaves_the_block_out(self) -> None:
        card = format_comment(replace(COMMENT, body="   "))

        assert "**monalisa** commented" in card.blocks[0].text
        assert BlockKind.BODY not in [block.kind for block in card.blocks]

    def test_a_linked_commenter_is_mentioned(self) -> None:
        assert "<@7>" in format_comment(COMMENT, {"monalisa": 7}).text

    def test_a_deleted_account_does_not_crash(self) -> None:
        message = format_comment(replace(COMMENT, author=None)).text

        assert "Unknown" in message

    def test_a_mass_mention_in_the_body_cannot_resolve(self) -> None:
        """Broken here as well as suppressed by the client, so it reads as text and pings nobody."""
        message = format_comment(replace(COMMENT, body="@everyone look at this")).text

        assert "@everyone" not in message
        assert "look at this" in message

    def test_a_user_mention_in_the_body_cannot_resolve(self) -> None:
        """The one form the client is told to honour, so quoting alone would let it through."""
        message = format_comment(replace(COMMENT, body="ping <@1234567> about this")).text

        assert "<@1234567>" not in message
        assert "1234567" in message

    def test_markup_in_the_body_does_not_restyle_the_thread(self) -> None:
        message = format_comment(replace(COMMENT, body="**bold** and `code`")).text

        assert "**bold**" not in message
        assert "bold" in message


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
        message = format_review(REVIEW).text

        assert "**monalisa** approved this pull request" in message
        assert f"<t:{int(UPDATED.timestamp())}:f>" in message
        assert "pullrequestreview-555" in message

    def test_the_body_is_a_block_of_its_own(self) -> None:
        assert format_review(REVIEW).blocks[1].text == "Looks right, one nit inline."

    def test_changes_requested_reads_as_such(self) -> None:
        message = format_review(replace(REVIEW, state="changes_requested")).text

        assert "**monalisa** requested changes" in message

    def test_a_plain_comment_review_reads_as_a_review(self) -> None:
        assert "left a review" in format_review(replace(REVIEW, state="commented")).text

    def test_an_uppercase_state_still_resolves(self) -> None:
        """The REST API sends APPROVED, so the verdict has to be case insensitive."""
        assert "approved this pull request" in format_review(replace(REVIEW, state="APPROVED")).text

    def test_an_unknown_state_falls_back_to_reviewed(self) -> None:
        assert "**monalisa** reviewed" in format_review(replace(REVIEW, state="whatever")).text

    def test_an_empty_body_still_carries_the_verdict(self) -> None:
        """Approving with no comment is the common case and still worth announcing."""
        card = format_review(replace(REVIEW, body=""))

        assert "approved this pull request" in card.blocks[0].text
        assert BlockKind.BODY not in [block.kind for block in card.blocks]

    def test_a_long_body_is_truncated(self) -> None:
        message = format_review(replace(REVIEW, body="x" * 5000)).text

        assert message.count("x") == COMMENT_PREVIEW_LIMIT

    def test_a_linked_reviewer_is_mentioned(self) -> None:
        assert "<@7>" in format_review(REVIEW, {"monalisa": 7}).text

    def test_a_deleted_account_does_not_crash(self) -> None:
        assert "Unknown" in format_review(replace(REVIEW, author=None)).text


class TestMarkupGluedToALink:
    """`escape_markdown` skips whatever its URL pattern matches, and that runs to the next space.

    Left at its default, the escaping this module relies on has a hole exactly the width of a
    URL: anything markdown-shaped stuck to the end of one reaches Discord intact.
    """

    def test_a_fence_stuck_to_a_url_cannot_open_a_code_block(self) -> None:
        """An unclosed fence eats the rest of the message, including the link back to GitHub."""
        hostile = replace(COMMENT, body="lgtm https://example.com/```\n**SHIPPED BY ADMIN**")

        rendered = format_comment(hostile).text

        assert "```" not in rendered
        assert "**SHIPPED BY ADMIN**" not in rendered

    def test_the_link_back_to_github_is_still_a_link(self) -> None:
        """The cost of escaping links is paid by the preview, not by the pointer that matters."""
        assert f"<{COMMENT.html_url}>" in format_comment(COMMENT).text
