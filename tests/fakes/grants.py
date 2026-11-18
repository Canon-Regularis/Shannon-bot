"""Whether GitHub vouches for a member the role turned away, stood in for. Issue #94.

The real answer is `GitHubTierGrants`, which reads a proof and a repository and asks GitHub, and
its own integration tests are where that is checked. Everything that only needs to know what a
guard or a gate does with an answer gets this instead, told the answer outright.
"""

from __future__ import annotations

from tests.fakes.discord_objects import FakeInteraction


class FakeGrants:
    """A refusal, a grant (`refusal=None`) or GitHub not answering, and every question written down.

    Refuses by default, unlike `FakeTiers` beside it. That one stands in for a check most tests
    are not about, so holding everything keeps it out of the way; this one raises access, and a
    default that let people in would pass a test that never meant to grant anything.

    `watching` is the interaction a guard is answering, when a test wants to know whether it had
    already been deferred by the time GitHub was asked. Deferring first is the whole of what keeps
    a slow GitHub from costing a member Discord's three seconds.
    """

    def __init__(
        self,
        refusal: str | None = "GitHub's write permission would let you in as well.",
        *,
        error: Exception | None = None,
        watching: FakeInteraction | None = None,
    ) -> None:
        self.refusal = refusal
        self.error = error
        self.watching = watching
        self.asked: list[tuple[int, int, str]] = []
        self.deferred_when_asked: list[bool] = []

    async def refusal_for(self, *, guild_id: int, discord_user_id: int, level: str) -> str | None:
        self.asked.append((guild_id, discord_user_id, level))
        if self.watching is not None:
            self.deferred_when_asked.append(self.watching.response.deferred)
        if self.error is not None:
            raise self.error
        return self.refusal
