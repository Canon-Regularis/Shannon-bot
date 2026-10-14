"""One run at a time per key, and a wait after each one finishes. Issue #185.

`/refresh` sweeps a whole repository and `/regenerate` redraws one thread. Both are cheap to ask
for and not cheap to do, since every run spends Discord's patience and the database's time, so each
takes turns: one run per key at a time - a server for `/refresh`, a thread for `/regenerate` - and
the next no sooner than a set wait after the last one FINISHED. From the finish rather than the
start, so a long run can never have the next one begin underneath it.

Not discord.py's `app_commands.checks.cooldown`. That one runs before the command's own checks, so
somebody without the role would spend the server's turn; it counts from when a command was asked
for rather than when it finished; it has no clock a test can move; and what it raises reaches the
tree's handler as "Something went wrong here", which is not what a wait is.

Kept in memory, the way the board poller keeps its spacing. This is one process, and a restart
costs at most one run sooner than the wait allowed, never a wrong answer.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NotYet:
    """Why a run may not start yet: another is under way, or the wait after the last is not up."""

    running: bool
    # Whole seconds still to wait, rounded up so a wait it promises is never too short. Nought
    # while a run is under way, because nobody can know how long that one has left.
    seconds: int = 0


class Cooldown:
    """One run per key at a time, and the next no sooner than `spacing` seconds after the last."""

    def __init__(self, spacing: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._spacing = spacing
        # Monotonic, as the poller's spacing is: a wall clock stepped back by an NTP correction
        # would hold every run back by the size of the step.
        self._clock = clock
        self._running: set[int] = set()
        self._finished: dict[int, float] = {}

    def begin(self, key: int) -> NotYet | None:
        """Start a run for `key`, or say why not. None means it has begun, and `finish` is owed.

        A refusal changes nothing. Above all it never ends the run that is under way, which the
        caller must not `finish` for it either.
        """
        now = self._clock()
        # Spent waits go on the way in, the way `OwnerBoards` drops listings that have expired,
        # so a key used once is not held for the life of the process.
        self._finished = {
            waiting: at for waiting, at in self._finished.items() if now - at < self._spacing
        }
        if key in self._running:
            return NotYet(running=True)
        finished = self._finished.get(key)
        if finished is not None:
            return NotYet(running=False, seconds=math.ceil(self._spacing - (now - finished)))
        self._running.add(key)
        return None

    def finish(self, key: int) -> None:
        """End the run for `key`, which starts its wait. Owed once for every `begin` that began."""
        self._running.discard(key)
        self._finished[key] = self._clock()


def seconds_in_words(seconds: int) -> str:
    """A wait the way a reply says it."""
    return "1 second" if seconds == 1 else f"{seconds} seconds"
