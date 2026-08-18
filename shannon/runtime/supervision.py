"""Starting, watching and stopping the tasks that run beside the API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Shutdown:
    """Whether the process asked for what is about to happen.

    A background task ending is an emergency or a formality, and nothing about the task says
    which.
    """

    asked: bool = False


def report_exit(what: str, shutdown: Shutdown):
    """Say why a background task stopped, when nobody asked it to.

    A task that dies otherwise takes its exception with it while the endpoint carries on
    answering.

    Quiet once a stop has been asked for, since a clean shutdown ends these tasks exactly as a
    failure does: the worker's loop returns when told, and the Discord client's start returns
    when closed. Warning either way meant the one line that says the process is now useless
    appeared on every restart. Errors are still reported whichever way it is going.
    """

    def report(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("the %s stopped: %s", what, error, exc_info=error)
        elif not shutdown.asked:
            logger.warning("the %s stopped without an error", what)

    return report


async def safely(what: str, closing: Awaitable[None]) -> None:
    """Run one shutdown step, reporting a failure rather than raising it.

    One step failing must not take the rest with it: a database pool, an HTTP client and a
    gateway connection all need closing.
    """
    try:
        await closing
    except Exception as error:
        logger.error("could not %s while shutting down: %s", what, error)


async def stop(task: asyncio.Task | None, *, grace: float = 0.0) -> None:
    """Wait `grace` seconds for a task to finish on its own, then cancel it.

    Watches the task rather than awaiting its result. Awaiting adopts the exception of a task
    that has already died, which would raise out of the first step of shutdown and leave
    everything after it unclosed. The done callback has already reported it.
    """
    if task is None:
        return

    if grace > 0:
        await asyncio.wait({task}, timeout=grace)
        if task.done():
            return
        logger.warning("a background task did not stop within %ss, cancelling it", grace)

    task.cancel()
    await asyncio.wait({task})
