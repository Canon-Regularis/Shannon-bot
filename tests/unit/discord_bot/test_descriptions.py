"""The description an item was opened with, where the block puts it and when it leaves it out.

The block said what an item was called, who was on it and what state it was in, and nothing about
what it was for. This is that missing part. Issue #75.

Most of what is here is about the cases where there is nothing to show, because those are the ones
that read as a broken bot rather than as a missing field: a label standing over an empty quote is
worse than no label, and the block has three separate ways of arriving at one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import format_issue, format_pull_request
from shannon.discord_bot.panels import PANEL_BUDGET
from shannon.discord_bot.safe_text import DESCRIPTION_PREVIEW_LIMIT
from shannon.domain.enums import Priority, Status
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)

pytestmark = pytest.mark.unit

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
COMMON = {
    "repository": REPO,
    "github_object_id": 100,
    "number": 79,
    "title": "feat: add ability to ping tagged user",
    "html_url": "https://github.com/Canon-Regularis/Shannon-bot/pull/79",
    "state": "open",
    "author": Actor("Requiem-Saraph"),
    "updated_at": datetime(2026, 9, 16, 0, 27, tzinfo=UTC),
}
PULL_REQUEST = PullRequestSnapshot(**COMMON)
ISSUE = IssueSnapshot(**COMMON)

LABEL = "**Description:**"


def block(body: str, snapshot: PullRequestSnapshot | IssueSnapshot = PULL_REQUEST) -> str:
    """The card's words, cut to the budget, which is what a reader sees.

    Trimmed here rather than raw, because trimming moved out of the renderer and into the panel
    when the block became a card: the renderer says everything and the send decides what fits.
    """
    render = format_issue if isinstance(snapshot, IssueSnapshot) else format_pull_request
    card = render(replace(snapshot, body=body), status=Status.NOT_REVIEWED, priority=Priority.UNSET)
    return card.trimmed().text


class TestWhenThereIsSomethingToShow:
    def test_it_is_labelled_and_left_as_written(self) -> None:
        """Unquoted since issue #113, which asked for the `> ` markers to go and nothing
        else. The label stays: `requirements.md` lists it and it is not what the issue was
        about."""
        rendered = block("Lets a tag in a comment reach the person.")

        assert f"{LABEL}\nLets a tag in a comment reach the person." in rendered
        assert "> " not in rendered.split(LABEL)[1], "the markers #113 objected to are back"

    def test_it_comes_after_everything_else(self) -> None:
        """The fields are what a reader scanning a channel wants; this is the prose under them.
        It is also what `fit` drops first, which is the right thing to lose."""
        rendered = block("why this exists")

        assert rendered.index("**Last Updated:**") < rendered.index(LABEL)

    def test_an_issue_gets_one_too(self) -> None:
        assert LABEL in block("why this exists", ISSUE)

    def test_markdown_reads_as_prose_rather_than_backslashes(self) -> None:
        """The whole reason the flattening exists. Discord's escaper puts a backslash in front of
        a line-leading hash and a line-leading dash, so an ordinary description arrived as a wall
        of them with a stray one on a line of its own."""
        rendered = block("## Summary\n\n- one\n- two")

        assert f"{LABEL}\nSummary\n\n• one\n• two" in rendered
        assert "\\" not in rendered.split(LABEL)[1]


class TestWhenThereIsNothingToShow:
    @pytest.mark.parametrize(
        "body",
        ["", "   ", "\n\n\n", "## ", "<!-- just a template comment -->"],
        ids=["empty", "spaces", "newlines", "a bare heading", "a comment"],
    )
    def test_no_label_and_no_section(self, body: str) -> None:
        """Asked of the rendered text and never of the body. A body of whitespace is truthy, and
        so is one of nothing but markdown markers, and either would leave the label standing over
        an empty quote."""
        rendered = block(body)

        assert LABEL not in rendered
        assert rendered.rstrip().endswith(":f>"), "the block should end on Last Updated"


class TestNothingUntrustedGetsThrough:
    """A description is written by whoever opened the item, which on a public repository is
    anybody. It is escaped exactly as a comment body is, and never resolved into a mention: the
    block is rewritten on every delivery, and a mention is an event rather than a standing field.
    """

    @pytest.mark.parametrize(
        "written", ["<@1234567890>", "<@&999>", "@everyone", "@here", "**SHIPPED BY ADMIN**"]
    )
    def test_it_cannot_ping_or_restyle(self, written: str) -> None:
        rendered = block(f"please look {written} now")

        assert written not in rendered
        assert "now" in rendered

    def test_a_linked_name_is_still_only_text(self) -> None:
        """A name in a comment resolves. A name here does not, because this line is rewritten
        every time anything about the item changes."""
        rendered = format_pull_request(
            replace(PULL_REQUEST, body="ask @octocat"),
            status=Status.NOT_REVIEWED,
            mentions={"octocat": 4242},
        )

        assert "<@4242>" not in rendered.text.split(LABEL)[1]

    def test_it_cannot_forge_a_field(self) -> None:
        """The block is read by eye as `**Label:** value` rows, and a description sits inside it."""
        rendered = block("**Status:** DONE")

        assert rendered.count("**Status:** DONE") == 0
        assert "**Status:** NOT_REVIEWED" in rendered

    def test_the_bold_markers_stay_balanced(self) -> None:
        """A block built of matched pairs with an odd number in it restyles every line below."""
        for written in ("**", "*", "***unclosed", "a ** b"):
            assert block(written).count("**") % 2 == 0, written


class TestWhenThereIsTooMuch:
    def test_a_long_description_is_cut_and_marked(self) -> None:
        rendered = block("w" * (DESCRIPTION_PREVIEW_LIMIT + 200))

        # Counted in the section rather than the whole block, because `Reviewers` has a w in it.
        assert rendered.endswith("…")
        assert rendered.split(LABEL)[1].count("w") == DESCRIPTION_PREVIEW_LIMIT

    def test_the_block_still_fits_a_card(self) -> None:
        assert len(block("*" * DESCRIPTION_PREVIEW_LIMIT)) <= PANEL_BUDGET

    def test_a_card_with_no_room_drops_the_description_whole(self) -> None:
        """The label can no longer be left standing over nothing, and not because somebody is
        careful about it. The label and the text under it are ONE block, and a panel over budget
        drops whole blocks from the end, so the two cannot be separated by a trim at all.

        This used to be arranged by arithmetic in `_with_the_description`, which measured the
        assembled string and threw the description away if the pair did not fit. The rule is the
        same and the structure now enforces it.
        """
        crowded = replace(PULL_REQUEST, title="T" * 4000, body="w" * DESCRIPTION_PREVIEW_LIMIT)

        rendered = format_pull_request(crowded, status=Status.NOT_REVIEWED).trimmed()

        assert LABEL not in rendered.text, "a label was left standing over nothing"
        assert rendered.length() <= PANEL_BUDGET

    def test_everything_at_once_still_fits_discord(self) -> None:
        """Every field at its widest and a description on top, which is the shape that actually
        overflows. Written out rather than left to the property tests: two hundred generated
        examples never put a full block and a full description together, so with the trimming
        taken out they went on passing while this renders at over two thousand characters.
        """
        crowded = replace(
            PULL_REQUEST,
            title="t" * 200,
            assignees=tuple(Actor("a" * 40) for _ in range(30)),
            reviewers=tuple(Actor("r" * 40) for _ in range(30)),
            labels=tuple(Label("l" * 200) for _ in range(30)),
            body="w" * 3000,
        )

        assert format_pull_request(crowded, status=Status.NOT_REVIEWED).trimmed().length() <= (
            PANEL_BUDGET
        )

    def test_a_description_that_does_fit_is_shown_whole(self) -> None:
        """The other side of the same branch, or the test above passes against a block that never
        shows a description at all."""
        rendered = block("w" * 400)

        assert LABEL in rendered
        assert rendered.split(LABEL)[1].count("w") == 400
