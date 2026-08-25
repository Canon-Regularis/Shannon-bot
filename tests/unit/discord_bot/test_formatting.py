from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import format_pull_request
from shannon.discord_bot.safe_text import MESSAGE_LIMIT, as_plain_text
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


def test_truncation_never_leaves_bold_hanging_open() -> None:
    """A cut inside `**` turns the rest of the message into one long bold run."""
    huge = replace(SNAPSHOT, reviewers=tuple(Actor(f"reviewer{index}") for index in range(400)))

    message = format_pull_request(huge, status=Status.NOT_REVIEWED)

    assert len(message) <= MESSAGE_LIMIT
    assert message.count("**") % 2 == 0


def test_truncation_keeps_whole_lines() -> None:
    """Every kept line must be one the untruncated block actually contains, start to finish."""
    huge = replace(SNAPSHOT, reviewers=tuple(Actor(f"reviewer{index}") for index in range(400)))

    message = format_pull_request(huge, status=Status.NOT_REVIEWED)

    kept = message.removesuffix("\n…").split("\n")
    # Checked against the shape a line must have, not against the function's own output, or
    # this compares the truncated block with itself and passes whatever truncation does.
    assert len(kept) < 11, "nothing was dropped, so this proves nothing"
    for line in kept:
        assert line.startswith("**") and ":** " in line, f"a line was cut in half: {line[:60]!r}"
    assert kept[0].startswith("**PR Name:**")


def test_truncation_drops_from_the_end() -> None:
    """The lines that survive are the first ones, not an arbitrary subset."""
    huge = replace(SNAPSHOT, reviewers=tuple(Actor(f"reviewer{index}") for index in range(400)))

    message = format_pull_request(huge, status=Status.NOT_REVIEWED)

    kept = message.removesuffix("\n…").split("\n")
    labels = [line.split(":**")[0] for line in kept]
    assert (
        labels
        == ["**PR Name", "**Type", "**State", "**GitHub Link", "**Author", "**Assignees"][
            : len(labels)
        ]
    )


def test_a_single_line_longer_than_the_whole_limit_is_still_cut() -> None:
    """No boundary to cut on, so the hard cut is the only option left."""
    huge = replace(SNAPSHOT, title="x" * 5000, labels=())

    message = format_pull_request(huge, status=Status.NOT_REVIEWED)

    assert len(message) <= MESSAGE_LIMIT
    assert message.endswith("…")


def test_a_label_containing_a_backtick_keeps_its_code_span() -> None:
    """GitHub allows a backtick in a label name; a single-backtick span would close early."""
    quoted = replace(SNAPSHOT, labels=(Label("needs `review`"), Label("bug")))

    fields = lines(format_pull_request(quoted, status=Status.NOT_REVIEWED))

    # Padding goes on both ends because markdown only strips a space from each side as a pair.
    assert fields["Tags"] == "`` needs `review` ``, `bug`"


@pytest.mark.parametrize("label", ["a``b", "a```b", "```", "``````x", "`" * 20])
def test_a_label_cannot_open_a_code_block_in_the_metadata(label: str) -> None:
    """Markdown answers a backtick inside a span with a longer fence, and Discord does not read
    it that way: three backticks there open a code BLOCK. A label carrying two of them turned
    the Tags line into a block, and one carrying three closed that block early and left the rest
    of the message rendering as whatever came after it.
    """
    labelled = replace(SNAPSHOT, labels=(Label(label),))

    message = format_pull_request(labelled, status=Status.NOT_REVIEWED)

    assert "```" not in message, f"{label!r} put a code block fence in the metadata"


def test_a_label_cannot_smuggle_a_working_mention_into_the_thread() -> None:
    """A label is GitHub-authored text, and `<@id>` is the one mention form that still pings.

    The code span around a label is a markdown rendering, and `allowed_mentions` is not reading
    markdown: it is the delivery gate, it is told to honour user mentions, and it reads the
    content it is handed. Every other untrusted field here is defused before it goes out; this
    one was left to the span.
    """
    labelled = replace(SNAPSHOT, labels=(Label("<@1234567890>"),))

    fields = lines(format_pull_request(labelled, status=Status.NOT_REVIEWED))

    assert "<@1234567890>" not in fields["Tags"], "a label name pinged whoever it named"
    assert "1234567890" in fields["Tags"], "the label should still read as what was written"


def test_defusing_a_label_leaves_an_ordinary_one_alone() -> None:
    fields = lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))

    assert fields["Tags"] == "`backend`, `bug`"


def test_a_missing_title_reads_unknown() -> None:
    fields = lines(format_pull_request(replace(SNAPSHOT, title=""), status=Status.NOT_REVIEWED))

    assert fields["PR Name"] == "Unknown"


class TestMarkupGluedToALink:
    """`escape_markdown` skips whatever its URL pattern matches, and that runs to the next space.

    Left at its default, the escaping this module relies on has a hole exactly the width of a
    URL: anything markdown-shaped stuck to the end of one is handed to Discord intact.
    """

    def test_a_bold_marker_stuck_to_a_url_cannot_unbalance_the_block(self) -> None:
        """Bold runs past a newline, so an odd marker re-pairs every label with the wrong value."""
        titled = replace(SNAPSHOT, title="Fix https://a.com/**")

        rendered = format_pull_request(titled, status=Status.NOT_REVIEWED)

        assert rendered.count("**") % 2 == 0

    def test_the_link_back_to_github_is_still_a_link(self) -> None:
        """The cost of escaping links is paid by previews, not by the pointer that matters."""
        rendered = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED)

        assert SNAPSHOT.html_url in rendered


# Titles carrying a markdown link and one loose marker after it, which is the shape the greedy
# link alternative in `escape_markdown` lets through.
LINKED_TITLES = [
    "Fix [regression](https://github.com/o/r/issues/3) in **/*.py (again)",
    "Fix [a](https://x.dev) the *thing (typo)",
    "Ignore [vendored](https://x.dev) __pycache__ dirs (cleanup)",
    "Drop [the](https://x.dev) ~~old~~ path (v2)",
    "See [this](https://x.dev) ||spoiler|| (maybe)",
    "Fixed in [abc123](https://x.dev/c) then ``` (end)",
]


class TestMarkupAfterAMarkdownLink:
    """The second hole in the same escaping, and the wider of the two.

    `escape_markdown` escapes one character at a time except for `[text](url)`, which is an
    alternative in its pattern that matches a span. It is greedy, so on a line carrying one it
    runs from the first bracket to the last closing parenthesis on that line, puts one backslash
    in front of all of it, and everything in between goes to Discord unescaped. Turning
    `ignore_links` off closed the other hole and does nothing for this one.

    The block below is built entirely out of matched pairs, so an odd marker leaking through
    re-pairs every label with the value of the field under it, and whoever wrote the title
    chooses where that starts.
    """

    @pytest.mark.parametrize("title", LINKED_TITLES)
    def test_no_marker_survives_a_link_earlier_on_the_line(self, title: str) -> None:
        rendered = as_plain_text(title)

        loose = [
            index
            for index, character in enumerate(rendered)
            if character in "*_~|`" and (index == 0 or rendered[index - 1] != "\\")
        ]
        assert not loose, f"{rendered!r} carries markers Discord will act on"

    @pytest.mark.parametrize("title", LINKED_TITLES)
    def test_the_block_a_title_lands_in_stays_paired(self, title: str) -> None:
        """Bold runs past a newline, so an odd marker re-pairs every label below it."""
        rendered = format_pull_request(replace(SNAPSHOT, title=title), status=Status.NOT_REVIEWED)

        assert rendered.count("**") % 2 == 0

    def test_the_pointer_back_to_github_survives_a_fence_in_the_title(self) -> None:
        """A live fence opens a code block that runs to the end of the message."""
        titled = replace(SNAPSHOT, title="Fixed in [abc123](https://x.dev/c) then ``` (end)")

        rendered = format_pull_request(titled, status=Status.NOT_REVIEWED)

        assert SNAPSHOT.html_url in rendered
