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


def rows(
    status: Status = Status.IN_REVIEW,
    snapshot: TicketSnapshot = TICKET,
    mentions: dict[str, int] | None = None,
) -> dict[str, str]:
    """The one FIELDS block, read back as a mapping, and an error rather than a guess if the
    block is not the shape this assumes."""
    block = next(
        block
        for block in format_ticket(snapshot, status=status, mentions=mentions).blocks
        if block.kind is BlockKind.FIELDS
    )
    return dict(line.removeprefix("**").split(":** ", 1) for line in block.text.splitlines())


# A card with every board field set, which is what issue #182 made possible. The values are the
# shapes a real board answered with: a single-select priority of `HIGH`, a story point of `05`
# zero-padded rather than numbered, an iteration named `Iteration 1`, and an area of `DB`.
CARRYING = replace(
    TICKET,
    author=Actor("octocat", github_user_id=1, avatar_url="https://avatars.example/u/1"),
    assignees=(Actor("hubot"), Actor("monalisa")),
    labels=(Label("high priority"),),
    created_at=datetime(2026, 6, 15, 0, 16, 26, tzinfo=UTC),
    priority_name="HIGH",
    story_point="05",
    iteration="Iteration 1",
    area="DB",
)


class TestWhatACardCarries:
    """Issue #182. The board read asks for the board's own fields now, so a card has metadata.

    Before this, the read asked GitHub for Title and Status alone and every one of these rows was
    absent because there was nothing to put in it. The class below still covers that case, which is
    a card with the fields unset rather than a board without them - both still leave the row out.
    """

    def test_every_row_the_board_can_fill_is_there(self) -> None:
        """In the order a reader scans them: what it is, where it is, who it belongs to, where it
        stands, and when it moved."""
        assert list(rows(snapshot=CARRYING)) == [
            "Ticket Name",
            "Type",
            "GitHub Link",
            "Creator",
            "Assignees",
            "Status",
            "Priority",
            "Story Point",
            "Iteration",
            "Area",
            "Tags",
            "Created",
            "Last Updated",
        ]

    def test_the_values_are_the_board_own(self) -> None:
        said = rows(snapshot=CARRYING)

        assert said["Creator"] == "octocat"
        assert said["Assignees"] == "hubot, monalisa"
        assert said["Priority"] == "HIGH"
        assert said["Iteration"] == "Iteration 1"
        assert said["Area"] == "DB"

    def test_a_story_point_keeps_the_padding_the_board_gave_it(self) -> None:
        """`05`, not `5`. The board holds a single-select of zero-padded options rather than a
        number, and the padding is somebody's choice about their own board - reformatting it would
        be this bot second-guessing them, and would make the thread and the board disagree."""
        assert rows(snapshot=CARRYING)["Story Point"] == "05"

    def test_a_linked_person_is_a_mention(self) -> None:
        """A card names people now, so the mention map has somebody to resolve. It used to have
        nobody, which is why `format_ticket` ignored the map entirely."""
        said = rows(snapshot=CARRYING, mentions={"octocat": 909})

        assert said["Creator"] == "<@909>"
        assert said["Assignees"] == "hubot, monalisa", "only the linked one resolves"

    def test_the_creators_face_is_the_picture(self) -> None:
        """The one block in the project that never carried a thumbnail, because it never had an
        author to take one from."""
        card = format_ticket(CARRYING, status=Status.IN_REVIEW)

        assert card.thumbnail_url == "https://avatars.example/u/1"


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
    """A row with nothing to put in it, which is still left out. Issue #166's rule, still standing.

    What changed with #182 is WHY these are absent. They used to be unreachable: the read asked
    GitHub for Title and Status alone, so no card could carry any of them. Now the read asks, and
    `TICKET` is a card with those fields UNSET - a board that has not got the field, or a card
    nobody has filled in, reaches exactly the same place. A row is omitted rather than rendered
    `None`, because an always-empty field reads as data missing rather than data absent.
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

    def test_a_card_carrying_people_and_labels_now_shows_them(self) -> None:
        """This asserted the opposite until issue #182, and it was written to be the thing that
        caught this change: *"if the board read ever learns to fetch these, this test is what says
        the renderer has to be told too rather than quietly following."*

        The read learnt. It is the only test in the suite that failed when the read was widened,
        which is exactly the job it was given.
        """
        carrying = replace(
            TICKET,
            author=Actor("octocat"),
            assignees=(Actor("hubot"),),
            labels=(Label("bug"),),
        )

        said = rows(snapshot=carrying)

        assert said["Creator"] == "octocat"
        assert said["Assignees"] == "hubot"
        assert said["Tags"] == "`bug`"


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
