"""The block at the top of a project ticket's thread. Issue #166.

There was no unit test of `format_ticket` at all before this file: the block was covered only end
to end, through a board poll, by one assertion comparing its three lines against a list. That is
why the duplication the issue complains about could sit here and drift - the labels were written
out a second time in this renderer, and nothing was watching the two copies agree.

The rows go through `_rows` now, which the pull request and issue blocks share. What a ticket SHOWS
is still decided by what a draft card has, and most of this file is about that: the board read
fetches Title and Status and nothing else, so the rows about people and labels are left out rather
than rendered empty. An always-empty field reads as data missing rather than data absent, which is
the same rule that keeps a reviewers line off an issue.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from shannon.discord_bot.formatting import format_ticket
from shannon.discord_bot.panels import Accent, BlockKind
from shannon.domain.enums import Priority, Status
from shannon.domain.models import Actor, Label, RepositorySnapshot, TicketSnapshot

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
UPDATED = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
BOARD = "https://github.com/users/Canon-Regularis/projects/6"

# What `snapshot_of` builds out of a board read, and only that. The author, assignees, labels and
# body it inherits are left at their empty defaults deliberately: the board read never fills them,
# so a fixture that did would be testing a card GitHub does not send.
TICKET = TicketSnapshot(
    repository=REPO,
    github_object_id=901,
    number=6,
    title="Write the poller",
    html_url=BOARD,
    state="open",
    updated_at=UPDATED,
    column="In Progress",
    project_number=6,
)


def rows(status: Status = Status.IN_REVIEW, snapshot: TicketSnapshot = TICKET) -> dict[str, str]:
    """The one FIELDS block, read back as a mapping, and an error rather than a guess if the
    block is not the shape this assumes."""
    block = next(
        block
        for block in format_ticket(snapshot, status=status).blocks
        if block.kind is BlockKind.FIELDS
    )
    return dict(line.removeprefix("**").split(":** ", 1) for line in block.text.splitlines())


class TestWhatATicketShows:
    def test_the_rows_are_the_ones_a_draft_card_can_fill(self) -> None:
        """In the order `_metadata` puts the same labels in, because they come off one builder."""
        assert list(rows()) == ["Ticket Name", "Type", "GitHub Link", "Status", "Last Updated"]

    def test_the_values_come_from_the_card(self) -> None:
        assert rows() == {
            "Ticket Name": "Write the poller",
            "Type": "Ticket",
            "GitHub Link": BOARD,
            "Status": "In review",
            "Last Updated": f"<t:{int(UPDATED.timestamp())}:f>",
        }

    def test_the_status_comes_from_the_caller_rather_than_the_column(self) -> None:
        """The board's column name is not one of this project's statuses; the mapping lives with
        the policies, and by here it has already been made."""
        assert rows(status=Status.DONE)["Status"] == "Done"

    def test_a_card_with_nothing_in_its_title_reads_unknown(self) -> None:
        assert rows(snapshot=replace(TICKET, title="   "))["Ticket Name"] == "Unknown"

    def test_a_board_that_did_not_say_when_reads_unknown(self) -> None:
        """Rather than being left out. Not saying is the honest answer and the row is one every
        other block carries, so the gap is worth showing."""
        assert rows(snapshot=replace(TICKET, updated_at=None))["Last Updated"] == "Unknown"


class TestWhatATicketLeavesOut:
    """Each of these would read `None` for ever, which is noise rather than information.

    Not an oversight in the board read either, in most cases: `/priority` refuses a ticket
    outright because a draft card has no labels to set, and the read asks GitHub for Title and
    Status only - naming more would send a field per card per poll that nothing parses.
    """

    def test_there_is_no_author_or_assignees_row(self) -> None:
        assert "Author" not in rows()
        assert "Assignees" not in rows()

    def test_there_is_no_tags_row(self) -> None:
        assert "Tags" not in rows()

    def test_there_is_no_priority_row(self) -> None:
        assert "Priority" not in rows()

    def test_there_is_no_state_row(self) -> None:
        """The one left out for a different reason: a ticket's state is hard-coded open and
        nothing in the project can close it, because a board column is not a closed state. The
        row could only ever say `Open`."""
        assert "State" not in rows()

    def test_a_card_carrying_people_and_labels_anyway_still_shows_none_of_them(self) -> None:
        """The rows are left out by not being built, not filtered on being empty - so a card that
        somehow arrived with an author would not start showing one. That is deliberate: if the
        board read ever learns to fetch these, this test is what says the renderer has to be told
        too rather than quietly following.
        """
        carrying = replace(
            TICKET,
            author=Actor("octocat"),
            assignees=(Actor("hubot"),),
            labels=(Label("bug"),),
        )

        assert list(rows(snapshot=carrying)) == list(rows())


class TestTheCardItIsDrawnOn:
    def test_it_is_grey(self) -> None:
        """A draft on a board has no state of its own to colour by."""
        assert format_ticket(TICKET, status=Status.IN_REVIEW).accent is Accent.DRAFT

    def test_it_never_carries_a_picture(self) -> None:
        """The only block with no author, so the only one with no face to show."""
        assert format_ticket(TICKET, status=Status.IN_REVIEW).thumbnail_url is None

    def test_there_is_no_description_block(self) -> None:
        """Not withheld but impossible: a board item carries no body text at all, so there is
        nothing to render even where a row were wanted."""
        kinds = [block.kind for block in format_ticket(TICKET, status=Status.IN_REVIEW).blocks]

        assert kinds == [BlockKind.FIELDS]

    def test_the_button_opens_the_board(self) -> None:
        """A draft has no page of its own, so the board's is what there is to open."""
        link = format_ticket(TICKET, status=Status.IN_REVIEW).link

        assert link is not None and link.url == BOARD

    def test_priority_and_mentions_are_accepted_and_ignored(self) -> None:
        """The policies all render through one signature, and a ticket has nobody to mention."""
        card = format_ticket(
            TICKET, status=Status.IN_REVIEW, priority=Priority.HIGH, mentions={"octocat": 7}
        )

        assert "Priority" not in card.text
        assert "<@7>" not in card.text
