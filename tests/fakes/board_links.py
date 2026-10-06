"""A stand-in for linking the board a one-click link carries, for round trips that carry none.

Issue #201 made the verification service take one, and required: a default would be a way for a
deployment to hand out links that carry a board and then quietly link nothing. Most round trips in
the suite are identities, or a board authorised without a board chosen, and for those this is the
honest collaborator. Asked at all, it raises - which the callback logs and answers as a board not
linked, so the tests that pin that such a link links nothing read the outcome rather than this:
`test_one_click_board_linking.py`, `TestWhatALinkWithoutABoardDoes`.
"""

from __future__ import annotations

from shannon.services.boards import BoardLink


class NoBoardLinks:
    """`LinksTheBoardChosen` for a test whose links never carry a board."""

    async def assign(
        self,
        *,
        guild_id: int,
        project_number: int,
        typed_owner: str,
        acting: int,
        chosen_under: str | None = None,
    ) -> BoardLink:
        raise AssertionError("a link that carried no board tried to link one")
