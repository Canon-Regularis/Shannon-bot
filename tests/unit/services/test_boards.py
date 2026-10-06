"""What linking a board says, which Discord and a browser are both told.

`said` is one function for two readers since issue #201: the reply to `/board link`, and the page
a browser lands on after a one-click link. Two copies would drift, and the page is the one nobody
re-reads after it ships.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from shannon.github.projects import ProjectListing
from shannon.services.boards import LIFETIME, BoardLink, OwnerBoards, said

pytestmark = pytest.mark.unit


def link(*, number: int = 7, replaced: int | None = None, title: str = "Roadmap") -> BoardLink:
    return BoardLink(
        repo_name="Canon-Regularis/Shannon-bot",
        owner="acme",
        number=number,
        title=title,
        replaced=replaced,
    )


class TestWhatLinkingABoardSays:
    def test_it_names_the_repository_the_owner_the_number_and_the_title(self) -> None:
        assert said(link()).startswith(
            "Canon-Regularis/Shannon-bot now mirrors acme's board #7, Roadmap."
        )

    def test_a_swapped_board_names_the_one_it_replaced(self) -> None:
        """A swapped board reads as having done nothing when the number was a digit out."""
        assert "It was mirroring #3." in said(link(replaced=3))

    def test_the_same_board_again_says_nothing_about_replacing_it(self) -> None:
        """One click makes linking the board a server already mirrors the ordinary case, and
        "it was mirroring #7" about #7 would read as a mistake."""
        assert "It was mirroring" not in said(link(replaced=7))

    def test_a_first_board_says_nothing_about_replacing_one(self) -> None:
        assert "It was mirroring" not in said(link(replaced=None))

    def test_it_says_when_the_cards_arrive(self) -> None:
        """Otherwise an empty channel a second later reads as a link that did not work."""
        assert said(link()).endswith("Cards appear at the next poll rather than at once.")

    def test_the_same_number_under_another_owner_names_the_owner_it_left(self) -> None:
        """A board is a number under an account, so this IS a swap - every card pairing on the
        old one was forgotten - and the numbers alone would say it was the board it already
        had."""
        swapped = BoardLink(
            repo_name="Canon-Regularis/Shannon-bot",
            owner="acme",
            number=7,
            title="Roadmap",
            replaced=7,
            replaced_owner="Canon-Regularis",
        )

        assert "It was mirroring Canon-Regularis's board #7." in said(swapped)

    def test_the_same_owner_in_another_case_is_the_same_board(self) -> None:
        same = BoardLink(
            repo_name="Canon-Regularis/Shannon-bot",
            owner="acme",
            number=7,
            title="Roadmap",
            replaced=7,
            replaced_owner="ACME",
        )

        assert "It was mirroring" not in said(same)


@dataclass(frozen=True, slots=True)
class Grant:
    token: str
    github_login: str = "octocat"


class Grants:
    """`HoldsBoardAuthorisations` out of a dictionary keyed by (guild, member)."""

    def __init__(self, **tokens: str) -> None:
        self.tokens = {
            (1, int(member.removeprefix("m"))): token for member, token in tokens.items()
        }

    async def granted_to(self, *, guild_id: int, discord_user_id: int) -> Grant | None:
        token = self.tokens.get((guild_id, discord_user_id))
        return Grant(token=token) if token else None

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool:
        return self.tokens.pop((guild_id, discord_user_id), None) is not None


class Listing:
    """`ReadsProjects`, recording every owner it was asked to list and as whom."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def list_boards(self, owner: str, *, token: str) -> Sequence[ProjectListing]:
        self.asked.append((owner, token))
        return [ProjectListing(number=1, title=f"{owner}'s board")]

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        raise AssertionError("the picker never opens a board")


class TestThePickersMemory:
    """`OwnerBoards`, which a picker asks on every keystroke. Issue #201 keyed it per member, and
    the halves of that key a database test cannot easily see are pinned here."""

    async def test_two_owners_are_two_lists_for_one_member(self) -> None:
        """The owner is in the key too: one member typing a second owner is asking a different
        question, and answering it out of the first owner's list would offer the wrong boards."""
        listing = Listing()
        boards = OwnerBoards(listing, Grants(m5="gho_five"))

        first = await boards.listed("acme", guild_id=1, member=5)
        second = await boards.listed("globex", guild_id=1, member=5)

        assert listing.asked == [("acme", "gho_five"), ("globex", "gho_five")]
        assert [one.title for one in (*first, *second)] == ["acme's board", "globex's board"]

    async def test_a_list_is_remembered_across_other_members_asking(self) -> None:
        listing = Listing()
        boards = OwnerBoards(listing, Grants(m5="gho_five", m6="gho_six"))

        await boards.listed("acme", guild_id=1, member=5)
        await boards.listed("acme", guild_id=1, member=6)
        await boards.listed("acme", guild_id=1, member=5)

        assert listing.asked == [("acme", "gho_five"), ("acme", "gho_six")]

    async def test_an_expired_list_is_let_go_when_another_is_kept(self) -> None:
        """Keyed per member, the memory would otherwise gain an entry for everybody who ever opened
        the picker and never lose one. Nothing outside can see an entry that is merely kept, so
        this reads the memory itself."""
        clock = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        boards = OwnerBoards(Listing(), Grants(m5="gho_five", m6="gho_six"), now=lambda: clock)

        await boards.listed("acme", guild_id=1, member=5)
        clock = clock + LIFETIME + timedelta(seconds=1)
        await boards.listed("acme", guild_id=1, member=6)

        assert list(boards._held) == [("acme", 1, 6)]  # pyright: ignore[reportPrivateUsage]

    async def test_the_owner_is_remembered_without_its_case(self) -> None:
        listing = Listing()
        boards = OwnerBoards(listing, Grants(m5="gho_five"))

        await boards.listed("Acme", guild_id=1, member=5)
        await boards.listed("aCME", guild_id=1, member=5)

        assert len(listing.asked) == 1
