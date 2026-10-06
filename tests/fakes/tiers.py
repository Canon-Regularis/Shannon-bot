"""Whether a member still holds a tier, stood in for.

Found reviewing #201. A board link asks Discord again, when it is followed, for the tier the
command was gated on. Every test that follows a board link and is not about that question gets a
member who holds everything, so it reads as though the question were not there; the tests that
are about it say otherwise.
"""

from __future__ import annotations

from collections.abc import Collection

from shannon.discord_bot.roles import CommandRole


class FakeTiers:
    """Everybody holds every tier unless told otherwise, and every question is written down."""

    def __init__(self, *, held: bool = True, fails: Exception | None = None) -> None:
        self.held = held
        self.fails = fails
        self.asked: list[tuple[int, int, frozenset[CommandRole]]] = []

    async def holds(
        self, *, guild_id: int, discord_user_id: int, tiers: Collection[CommandRole]
    ) -> bool:
        self.asked.append((guild_id, discord_user_id, frozenset(tiers)))
        if self.fails is not None:
            raise self.fails
        return self.held
