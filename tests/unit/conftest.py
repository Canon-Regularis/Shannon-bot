"""The tier label, applied to the tier rather than to the files somebody remembered.

`integration` is on all 63 of its files; `unit` was on 32 of 88. So `-m unit` quietly meant
"about a third of the unit tests", which is worse than no selector at all: it reads as a gate and
is not one. Marking here is what makes the two markers mean the same kind of thing.

The hook is handed EVERY collected item, not only the ones under this directory, so the path test
is load-bearing. Without it this would mark the integration tier `unit` as well.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shannon.discord_bot import responses

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(pytest.mark.unit)


class Deletes:
    """Every delete a reply has waiting, without the wait. Issue #240.

    A reply that deletes itself waits in a task of `responses`' own. This stands in for the wait,
    so nothing sleeps for real, keeps how long each would have been, and lets the tasks run to the
    end when a test asks.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    async def settle(self) -> None:
        """Let every delete waiting so far run to the end, however it ends."""
        if responses._DELETES:
            await asyncio.wait(set(responses._DELETES))


@pytest.fixture
def deletes(monkeypatch: pytest.MonkeyPatch) -> Deletes:
    """A reply's wait before it deletes itself, stood in for. Here rather than beside one test file
    because two use it: the replies themselves, and `/remind`, which counts down with one."""
    standing_in = Deletes()
    monkeypatch.setattr(responses, "_wait", standing_in.wait)
    return standing_in
