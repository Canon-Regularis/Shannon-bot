from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shannon.discord_bot.formatting import (
    OPEN_ON_GITHUB,
    format_pull_request,
    format_thread_moved,
    format_thread_moving,
)
from shannon.discord_bot.panels import PANEL_BUDGET, Accent, BlockKind, Panel
from shannon.discord_bot.safe_text import as_plain_text
from shannon.domain.enums import Priority, Status
from shannon.domain.models import Actor, Label, PullRequestSnapshot, RepositorySnapshot

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
UPDATED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
AVATAR = "https://avatars.githubusercontent.com/u/583231?v=4"

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


def lines(block: Panel) -> dict[str, str]:
    """The field rows, read out of the one block that holds them.

    That block rather than the whole card, which is what this used to split. The description
    sits in a block of its own since issue #113, so its own lines can no longer arrive here
    looking like rows whose label happened to be missing.
    """
    result = {}
    for line in fields_in(block).split("\n"):
        label, _, value = line.partition(":** ")
        result[label.removeprefix("**")] = value
    return result


def fields_in(block: Panel) -> str:
    """The one FIELDS block, and an error rather than a guess if there is not exactly one."""
    (said,) = [part.text for part in block.blocks if part.kind is BlockKind.FIELDS]
    return said


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

    assert fields["Status"] == "Not reviewed"
    assert fields["Priority"] == "None"


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

    assert fields["Status"] == "In review"
    assert fields["Priority"] == "High"


def test_output_is_stable_for_the_same_input() -> None:
    first = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED).text
    second = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED).text

    assert first == second


def test_an_absurd_title_is_truncated_to_fit_discord() -> None:
    huge = replace(SNAPSHOT, title="x" * 5000)

    message = format_pull_request(huge, status=Status.NOT_REVIEWED).trimmed().text

    assert len(message) <= PANEL_BUDGET


def test_truncation_never_leaves_bold_hanging_open() -> None:
    """A cut inside `**` turns the rest of the message into one long bold run."""
    huge = replace(SNAPSHOT, reviewers=tuple(Actor(f"reviewer{index}") for index in range(400)))

    message = format_pull_request(huge, status=Status.NOT_REVIEWED).trimmed().text

    assert len(message) <= PANEL_BUDGET
    assert message.count("**") % 2 == 0


def test_truncation_keeps_whole_lines() -> None:
    """Every kept line must be one the untruncated block actually contains, start to finish."""
    huge = replace(SNAPSHOT, reviewers=tuple(Actor(f"reviewer{index}") for index in range(400)))

    message = format_pull_request(huge, status=Status.NOT_REVIEWED).trimmed().text

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

    message = format_pull_request(huge, status=Status.NOT_REVIEWED).trimmed().text

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

    message = format_pull_request(huge, status=Status.NOT_REVIEWED).trimmed().text

    assert len(message) <= PANEL_BUDGET
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

    message = format_pull_request(labelled, status=Status.NOT_REVIEWED).trimmed().text

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


@pytest.mark.parametrize("title", ["", "   ", "\t", "\n \n", "\u00a0"])
def test_a_title_with_nothing_in_it_reads_unknown(title: str) -> None:
    """Whitespace is a title as far as a truthiness check is concerned, and is not one.

    Three things render this title and they used to disagree. `thread_name` strips, so the
    thread was called `#7`; `TicketPolicy.thread_name` calls an untitled card an untitled card;
    and the block asked whether the string was truthy, which spaces are, so it rendered a label
    with nothing after it. A draft titled with spaces opened a thread named "Untitled ticket"
    whose first line named it nothing at all, and a field that renders blank reads as the bot
    having broken rather than as an item nobody named.

    The mapping layer refuses a title that is missing or empty outright, so whitespace is the
    one shape of it that reaches here carrying nothing.
    """
    fields = lines(format_pull_request(replace(SNAPSHOT, title=title), status=Status.NOT_REVIEWED))

    assert fields["PR Name"] == "Unknown"


class TestMarkupGluedToALink:
    """`escape_markdown` skips whatever its URL pattern matches, and that runs to the next space.

    Left at its default, the escaping this module relies on has a hole exactly the width of a
    URL: anything markdown-shaped stuck to the end of one is handed to Discord intact.
    """

    def test_a_bold_marker_stuck_to_a_url_cannot_unbalance_the_block(self) -> None:
        """Bold runs past a newline, so an odd marker re-pairs every label with the wrong value."""
        titled = replace(SNAPSHOT, title="Fix https://a.com/**")

        rendered = format_pull_request(titled, status=Status.NOT_REVIEWED).text

        assert rendered.count("**") % 2 == 0

    def test_the_link_back_to_github_is_still_a_link(self) -> None:
        """The cost of escaping links is paid by previews, not by the pointer that matters."""
        rendered = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED).text

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
        rendered = format_pull_request(
            replace(SNAPSHOT, title=title), status=Status.NOT_REVIEWED
        ).text

        assert rendered.count("**") % 2 == 0

    def test_the_pointer_back_to_github_survives_a_fence_in_the_title(self) -> None:
        """A live fence opens a code block that runs to the end of the message."""
        titled = replace(SNAPSHOT, title="Fixed in [abc123](https://x.dev/c) then ``` (end)")

        rendered = format_pull_request(titled, status=Status.NOT_REVIEWED).text

        assert SNAPSHOT.html_url in rendered


class TestSayingWhereAnItemWent:
    """The line left in a thread the item has been moved off. Issue #78.

    Discord cannot move a thread between channels, so the old one is the only place somebody
    looking for the item will look, and it is the only place this can be said.
    """

    def test_it_names_the_replacement_thread(self) -> None:
        """A thread rather than the channel it is in, because the thread is where somebody
        reading this wants to be taken. `<#id>` renders either."""
        assert format_thread_moved(4242) == (
            "-# This item is now mirrored in <#4242>. Nothing more will be posted in this thread."
        )

    def test_a_card_with_no_replacement_yet_names_the_channel(self) -> None:
        """A board card has no GitHub endpoint to rebuild it from, so its pointer is let go of and
        the poller opens the new thread on its next pass. Until then the channel is all there is
        to name, and it is enough to stop somebody waiting in a thread nothing will arrive in."""
        assert format_thread_moving(77) == (
            "-# This item will be mirrored in <#77> from now on. Nothing more will be posted here."
        )

    @pytest.mark.parametrize("line", [format_thread_moved(1), format_thread_moving(1)])
    def test_neither_claims_the_thread_is_locked(self, line: str) -> None:
        """Both are written before the lock is attempted, because posting reopens an archived
        thread and shutting first would be undone by the line itself. At that moment nobody knows
        whether the lock will land, and a server without Manage Threads would be told it cannot
        reply somewhere it can.
        """
        assert "locked" not in line

    @pytest.mark.parametrize("line", [format_thread_moved(1), format_thread_moving(1)])
    def test_both_are_subtext(self, line: str) -> None:
        """Quieter than the headers beside them: this is a signpost, not news."""
        assert line.startswith("-# ")


class TestTheCardTheBlockIsDrawnOn:
    """Issue #116. The bar, the face and the button, which are the whole of what a reader sees
    before a word of the block has been read."""

    @pytest.mark.parametrize(
        ("snapshot", "accent"),
        [
            (SNAPSHOT, Accent.OPEN),
            (replace(SNAPSHOT, draft=True), Accent.DRAFT),
            (replace(SNAPSHOT, state="closed"), Accent.CLOSED),
            (replace(SNAPSHOT, state="closed", merged=True), Accent.MERGED),
            (replace(SNAPSHOT, merged=True), Accent.MERGED),
        ],
    )
    def test_a_pull_request_carries_githubs_colour_for_its_state(
        self, snapshot: PullRequestSnapshot, accent: Accent
    ) -> None:
        """Merged before closed, because GitHub carries merging as a flag beside the state and a
        merged pull request is closed too. Red for both would lose the one distinction anybody
        scrolling a channel actually wants."""
        assert format_pull_request(snapshot, status=Status.NOT_REVIEWED).accent == accent

    def test_a_draft_is_grey_rather_than_a_paler_green(self) -> None:
        """It is the one state that says "not yet", so it reads as quieter than the open ones
        beside it rather than as another shade of the same thing."""
        draft = format_pull_request(replace(SNAPSHOT, draft=True), status=Status.NOT_REVIEWED)

        assert draft.accent != format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED).accent

    def test_the_authors_face_is_the_picture(self) -> None:
        with_picture = replace(SNAPSHOT, author=Actor("octocat", avatar_url=AVATAR))

        card = format_pull_request(with_picture, status=Status.NOT_REVIEWED)

        assert card.thumbnail_url == AVATAR

    def test_an_item_whose_author_github_does_not_know_has_no_picture(self) -> None:
        """GitHub answers with no account whenever the address is registered to nobody, and a
        broken image where a face should be is worse than no face."""
        card = format_pull_request(replace(SNAPSHOT, author=None), status=Status.NOT_REVIEWED)

        assert card.thumbnail_url is None

    def test_the_button_opens_the_item_on_github(self) -> None:
        card = format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED)

        assert card.link is not None
        assert card.link.url == SNAPSHOT.html_url
        assert card.link.label == OPEN_ON_GITHUB

    def test_the_link_row_survives_the_button_that_repeats_it(self) -> None:
        """`requirements.md` lists the row and two tests pin the field list, so the button is
        beside it rather than instead of it. The redundancy is cheaper than the churn."""
        assert lines(format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED))["GitHub Link"] == (
            SNAPSHOT.html_url
        )

    def test_a_url_discord_could_not_parse_gets_no_button_rather_than_no_message(self) -> None:
        """Discord refuses the WHOLE message over a link it cannot read, so an item whose URL
        arrived malformed would have no block at all. The row above still carries the address.
        """
        odd = replace(SNAPSHOT, html_url="javascript:alert(1)")

        card = format_pull_request(odd, status=Status.NOT_REVIEWED)

        assert card.link is None
        assert lines(card)["GitHub Link"] == "javascript:alert(1)"

    def test_the_block_is_never_sent_as_plain_text(self) -> None:
        """A plain panel goes out as a string and costs no components. This one has a bar, a
        button and usually a face, so it never can."""
        assert not format_pull_request(SNAPSHOT, status=Status.NOT_REVIEWED).is_plain
