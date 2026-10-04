"""Stand-ins for the two credential questions a board asks.

Issue #170 split "which token does a board use" into two questions with different answers: a
board's own reads belong to the board and happen with nobody present, and a write belongs to
whoever asked for it. One fake answers both, because a test almost never cares about the
difference - and the two tests that do care set the fields apart.

Answers a token by DEFAULT, so every test written before any of this existed behaves as it did.
An empty string is the interesting case and has to be asked for: it means nobody authorised, which
a read turns into a board that will not open and a write turns into a refusal.
"""

from __future__ import annotations


class FakeBoardCredentials:
    """`WhoTheBoardIsReadAs` and `WhoIsMovingTheCard`, with whatever answers a test wants."""

    def __init__(self, *, reads: str = "gho_board", writes: str = "gho_mover") -> None:
        self.reads = reads
        self.writes = writes
        # What was asked for, so a test can assert WHOSE credential went out rather than only
        # that one did - which is the whole of what this change is about.
        self.read_for: list[tuple[str, int]] = []
        self.wrote_for: list[tuple[int, int]] = []

    async def reading(self, owner: str, project_number: int) -> str:
        self.read_for.append((owner, project_number))
        return self.reads

    async def moving(self, *, guild_id: int, discord_user_id: int) -> str:
        self.wrote_for.append((guild_id, discord_user_id))
        return self.writes
