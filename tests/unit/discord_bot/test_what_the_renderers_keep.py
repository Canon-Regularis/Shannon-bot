"""What the renderers say today, pinned before issue #269 rearranges the code that says it.

Characterisation rather than specification: each assertion is the output the current code gives,
taken from it, so the refactor has something to be held to. The audit behind #269 names the
places it changes on purpose - a multi-line card value, a whitespace-only one, an unlinked team,
a commit with no message - and none of those shapes is pinned here, so a deliberate fix fails
nothing in this file while an accidental change to anything else does.

Only names that outlive the refactor are imported: nothing here reaches for `code_span` through
`safe_text`, or for the time helpers, which #269 moves to `timestamps`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from shannon.discord_bot.formatting import (
    format_back_to_draft,
    format_card_changed,
    format_check_results,
    format_everyone_approved,
    format_force_push,
    format_label_change,
    format_pull_request,
    format_ready_for_review,
    format_review,
    format_review_comment,
    format_reviewer_ping,
    format_team_ping,
)
from shannon.discord_bot.panels import (
    PANEL_BUDGET,
    Accent,
    Block,
    BlockKind,
    Panel,
    PanelImage,
    PanelLink,
)
from shannon.discord_bot.rich_text import Images, as_rich_text
from shannon.discord_bot.safe_text import (
    CARD_FIELD_LIMIT,
    REVIEW_PATH_LIMIT,
    TRUNCATED,
    as_plain_text,
    clipped_job,
    clipped_path,
    fit,
)
from shannon.domain.enums import Status
from shannon.domain.models import (
    Actor,
    ChangesAsked,
    CheckReport,
    CheckRun,
    FileAuthors,
    ForcePush,
    Label,
    LabelMove,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewCommentSnapshot,
    ReviewSnapshot,
)
from shannon.domain.text import ZERO_WIDTH_SPACE

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
# A name GitHub would never issue, so no commit address can be built into it and `_which_commit`
# falls back to its code span.
NO_ADDRESS = RepositorySnapshot(
    github_repo_id=3, owner="o", name="..", html_url="https://github.com/o/.."
)
WHEN = datetime(2026, 8, 11, 10, 30, tzinfo=UTC)
MONALISA = Actor("monalisa", 200)

# A ping written by whoever named the label, the path, the job or the commit.
PING = "<@123>"
# What it is once defused: a zero-width space inside the brackets, so Discord reads no mention.
DEFUSED = f"<{ZERO_WIDTH_SPACE}@123>"
# Any user or role mention Discord would deliver, which is what must not survive.
_LIVE_MENTION = re.compile(r"<@[!&]?\d+>")


class TestACardChangeLine:
    """`format_card_changed` for values on one line, which #269 leaves byte-identical."""

    def test_the_whole_text_is_a_heading_and_one_subtext_line_per_field(self) -> None:
        said = format_card_changed([("Priority", "HIGH", "LOW"), ("Points", "", "5")])

        assert said.text == "### 📋 Ticket updated\n-# Priority: HIGH → LOW\n-# Points: None → 5"

    def test_a_value_taken_away_reads_as_none_on_the_right(self) -> None:
        said = format_card_changed([("Points", "5", "")])

        assert said.text == "### 📋 Ticket updated\n-# Points: 5 → None"

    def test_it_is_a_blue_heading_with_its_lines_under_it(self) -> None:
        said = format_card_changed([("Priority", "HIGH", "LOW")])

        assert said.accent is Accent.SAID
        assert [block.kind for block in said.blocks] == [BlockKind.HEADING, BlockKind.SUBHEADING]
        assert said.blocks[1].text == "-# Priority: HIGH → LOW"

    def test_a_long_value_is_cut_to_the_field_limit(self) -> None:
        said = format_card_changed([("Title", "x" * (CARD_FIELD_LIMIT + 20), "short")])

        assert said.blocks[1].text == f"-# Title: {'x' * CARD_FIELD_LIMIT}… → short"

    def test_a_value_carrying_a_ping_or_markup_is_defused_and_escaped(self) -> None:
        written = "<@123> **x** @everyone"

        said = format_card_changed([("Title", written, "a")]).text

        assert said.endswith(f"-# Title: {as_plain_text(written)} → a")
        assert not _LIVE_MENTION.search(said)
        assert "@everyone" not in said
        assert "**x**" not in said


class TestEveryCodeSpanIsDefusedFirst:
    """A code span stops markdown reading a name, not Discord delivering the ping inside it.

    One test per call site the audit lists (a label name, a reviewed file, a review comment's
    path, the tags row, a failed job, a succeeded job and a commit with no subject), each driven
    through the public renderer that reaches it, so folding the seven copies of the recipe into
    one helper under #269 cannot drop the defusing from any of them.
    """

    def test_a_label_name(self) -> None:
        said = format_label_change(LabelMove(name=PING, added=True)).text

        assert said == f"🏷️ Tag `{DEFUSED}` added."

    def test_a_file_a_review_asked_changes_on(self) -> None:
        review = ReviewSnapshot(
            repository=REPO,
            item_number=7,
            review_id=555,
            html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#pullrequestreview-555",
            body="Needs work.",
            state="changes_requested",
            author=MONALISA,
            created_at=WHEN,
            changes_asked=ChangesAsked(files=(FileAuthors(f"{PING}.py", ()),)),
        )

        said = format_review(review).text

        assert f"`{DEFUSED}.py` made by Unknown" in said
        assert not _LIVE_MENTION.search(said)

    def test_the_file_an_inline_comment_sits_on(self) -> None:
        comment = ReviewCommentSnapshot(
            repository=REPO,
            item_number=7,
            comment_id=1,
            html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#discussion_r1",
            body="Look here.",
            path=f"{PING}.py",
            line=5,
            author=MONALISA,
            created_at=WHEN,
        )

        said = format_review_comment(comment).text

        assert f"`{DEFUSED}.py` L5" in said
        assert not _LIVE_MENTION.search(said)

    def test_a_tag_in_the_block_at_the_top_of_a_thread(self) -> None:
        snapshot = PullRequestSnapshot(
            repository=REPO,
            github_object_id=100,
            number=7,
            title="Add the webhook endpoint",
            html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
            state="open",
            author=Actor("octocat"),
            labels=(Label(PING),),
            updated_at=WHEN,
        )

        said = format_pull_request(snapshot, status=Status.NOT_REVIEWED).text

        assert f"**Tags:** `{DEFUSED}`" in said
        assert not _LIVE_MENTION.search(said)

    def test_a_job_that_failed(self) -> None:
        said = format_check_results(_report(_run(PING, "failure"))).text

        assert f"- `{DEFUSED}` <https://github.com/acme/widget/actions/runs/1/job/1>" in said
        assert not _LIVE_MENTION.search(said)

    def test_a_job_that_succeeded(self) -> None:
        said = format_check_results(_report(_run(PING, "success"))).text

        assert f"**Successful Jobs:** `{DEFUSED}`" in said
        assert not _LIVE_MENTION.search(said)

    def test_a_commit_whose_address_cannot_be_built(self) -> None:
        """Seven characters, so the short hash is the whole ping."""
        push = ForcePush(
            repository=NO_ADDRESS,
            pusher=Actor("octocat"),
            before="<@1234>" + "0" * 33,
            after="2" * 40,
        )

        said = format_force_push(push).text

        assert f"from `<{ZERO_WIDTH_SPACE}@1234>` to `2222222`" in said
        assert not _LIVE_MENTION.search(said)


def _run(name: str, conclusion: str) -> CheckRun:
    return CheckRun(
        check_run_id=1,
        name=name,
        status="completed",
        conclusion=conclusion,
        html_url="https://github.com/acme/widget/actions/runs/1/job/1",
    )


def _report(*runs: CheckRun) -> CheckReport:
    return CheckReport(sha="c" * 40, runs=runs)


class TestWhoIsRung:
    """A linked person is `<@id>` and a linked team `<@&id>`, looked up by lowercased name.

    Pinned across the four renderers that build an audience, since #269 folds their four copies
    of that block into one. Unlinked teams are left out: #269 escapes them on purpose.
    """

    def test_a_linked_person_is_a_user_mention(self) -> None:
        said = format_reviewer_ping(["OctoCat"], {"octocat": 42})

        assert said.text == "Review requested from <@42>."
        assert said.accent is Accent.SAID

    def test_a_linked_team_is_a_role_mention(self) -> None:
        said = format_team_ping(["Core"], {"core": 9})

        assert said.text == "Review requested from <@&9>."
        assert said.accent is Accent.SAID

    def test_the_check_line_names_people_then_teams(self) -> None:
        said = format_check_results(
            _report(_run("Lint", "success")),
            people=(Actor("OctoCat"),),
            teams=(Actor("Core"),),
            mentions={"octocat": 42},
            roles={"core": 9},
        )

        assert said.blocks[1].text == "<@42> <@&9> Everything that ran passed."

    def test_the_ready_line_names_people_then_teams(self) -> None:
        said = format_ready_for_review(
            Actor("monalisa"),
            people=(Actor("OctoCat"),),
            teams=(Actor("Core"),),
            mentions={"octocat": 42},
            roles={"core": 9},
        )

        assert said.blocks[1].text == (
            "<@42> <@&9> **monalisa** marked this pull request ready for review."
        )

    def test_the_approved_line_names_people_then_teams(self) -> None:
        said = format_everyone_approved(
            2,
            people=(Actor("OctoCat"),),
            teams=(Actor("Core"),),
            mentions={"octocat": 42},
            roles={"core": 9},
        )

        assert said.blocks[1].text == (
            "<@42> <@&9> 2 reviews, all of them approving, and nobody is still being waited on."
        )

    def test_the_draft_line_never_rings_a_team_however_it_is_linked(self) -> None:
        said = format_back_to_draft(
            Actor("monalisa"),
            people=(Actor("OctoCat"),),
            teams=(Actor("core"),),
            mentions={"octocat": 42},
            roles={"core": 9},
        )

        assert said.blocks[1].text == (
            "<@42> core **monalisa** converted this pull request to draft."
        )


class TestCuttingAPathOrAJobFromTheFront:
    """`clipped_path` and `clipped_job`, which #269 gives one body: keep the end, never escape."""

    def test_a_path_under_the_limit_is_kept_as_written(self) -> None:
        assert clipped_path("  shannon/a_b*c.py  ") == "shannon/a_b*c.py"

    def test_a_path_at_the_limit_is_kept_whole(self) -> None:
        path = "p" * REVIEW_PATH_LIMIT

        assert clipped_path(path) == path

    def test_a_path_over_the_limit_loses_its_front(self) -> None:
        path = "front/" + "x" * (REVIEW_PATH_LIMIT - 7) + "end.py"

        clipped = clipped_path(path)

        assert len(clipped) == REVIEW_PATH_LIMIT
        assert clipped == "…" + path[-(REVIEW_PATH_LIMIT - 1) :]
        assert clipped.endswith("end.py")

    def test_a_job_under_the_limit_is_kept_as_written(self) -> None:
        assert clipped_job("  Tests (3.12)  ", limit=20) == "Tests (3.12)"

    def test_a_job_at_the_limit_is_kept_whole(self) -> None:
        assert clipped_job("j" * 20, limit=20) == "j" * 20

    def test_a_job_over_the_limit_loses_its_front(self) -> None:
        assert clipped_job("Tests (Python 3.12)", limit=10) == "…hon 3.12)"


class TestTrimmingAPanel:
    """`Panel.trimmed`, whose arithmetic #269 rewrites without changing any answer."""

    def test_a_panel_that_fits_is_unchanged(self) -> None:
        panel = Panel(
            blocks=(Block(BlockKind.HEADING, "h"), Block(BlockKind.BODY, "b" * 100)),
            accent=Accent.OPEN,
        )

        assert panel.trimmed() == panel

    def test_a_lone_block_over_the_budget_is_cut_on_a_line_boundary(self) -> None:
        text = "\n".join(f"{n:04d}" + "y" * 95 for n in range(60))
        panel = Panel.of_text(text)

        trimmed = panel.trimmed()

        (block,) = trimmed.blocks
        assert block.text == fit(text, limit=PANEL_BUDGET)
        assert block.text.endswith(TRUNCATED)
        assert len(block.text) <= PANEL_BUDGET
        assert text.startswith(block.text.removesuffix(TRUNCATED))

    def test_blocks_over_the_budget_together_are_dropped_whole_from_the_end(self) -> None:
        link = PanelLink("Open on GitHub", "https://github.com/o/r/pull/1")
        panel = Panel(
            blocks=(
                Block(BlockKind.HEADING, "heading"),
                Block(BlockKind.FIELDS, "f" * 2000),
                Block(BlockKind.BODY, "b" * 1950),
                Block(BlockKind.FOOTNOTE, "n" * 10),
            ),
            accent=Accent.MERGED,
            link=link,
        )

        trimmed = panel.trimmed()

        assert trimmed.blocks == panel.blocks[:2]
        assert (trimmed.accent, trimmed.link) == (Accent.MERGED, link)

    def test_a_first_block_too_big_on_its_own_is_cut_once_the_rest_are_gone(self) -> None:
        first = "\n".join("z" * 99 for _ in range(50))
        panel = Panel(
            blocks=(Block(BlockKind.HEADING, first), Block(BlockKind.BODY, "tail")),
            accent=Accent.OPEN,
        )

        trimmed = panel.trimmed()

        assert trimmed.blocks == (Block(BlockKind.HEADING, fit(first, limit=PANEL_BUDGET)),)


class TestAPictureInADescription:
    """`as_rich_text` lifting a GitHub-hosted markdown image into the gallery, as it does today."""

    def test_the_picture_is_lifted_and_its_alt_text_stays_in_the_prose(self) -> None:
        url = "https://github.com/user-attachments/assets/abc"

        described = as_rich_text(f"Before\n![a screenshot]({url})\nAfter")

        assert described.text == "Before\na screenshot\nAfter"
        assert described.images == (PanelImage(url=url, alt="a screenshot"),)

    def test_a_picture_with_no_alt_text_leaves_nothing_behind(self) -> None:
        url = "https://user-images.githubusercontent.com/1/x.png"

        described = as_rich_text(f"![]({url})")

        assert described.text == ""
        assert described.images == (PanelImage(url=url, alt=None),)

    def test_a_picture_hosted_anywhere_else_is_not_lifted_and_keeps_only_its_alt_text(
        self,
    ) -> None:
        described = as_rich_text("![x](https://evil.example/x.png)")

        assert described.text == "x"
        assert described.images == ()

    def test_asked_for_links_it_leaves_the_picture_where_it_was_as_one(self) -> None:
        url = "https://github.com/user-attachments/assets/abc"

        described = as_rich_text(f"Before\n![a screenshot]({url})\nAfter", images=Images.AS_LINKS)

        assert described.text == f"Before\n[a screenshot]({url})\nAfter"
        assert described.images == ()
