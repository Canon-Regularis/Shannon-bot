"""Waiting for something a background task does on its own schedule."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Wait for `condition`, rather than guessing at a sleep.

    Bounded, and on a real interval rather than `asyncio.sleep(0)`. A bare yield reschedules at
    once and never lets the loop block, so waiting out a tick that way costs the whole tick at
    full CPU on every run, and a condition that never arrives costs the job its ceiling with no
    test named.
    """
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
