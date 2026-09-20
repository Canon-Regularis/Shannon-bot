"""Starting, watching and stopping the tasks that run beside the API."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Shutdown:
    """Whether the process asked for what is about to happen.

    A background task ending is an emergency or a formality, and the task itself says neither.
    """

    asked: bool = False


def why(error: BaseException) -> str:
    """What went wrong, in words, even when the exception has none.

    `str(TimeoutError())` is the empty string, because asyncio raises it with no arguments, and an
    unanswered connection is the failure that most needs naming in the log.
    """
    return str(error) or type(error).__name__


def ask_the_process_to_stop() -> None:
    """Send this process the signal an orchestrator would send it.

    Uvicorn owns the exit, and the signal runs its ordinary shutdown: the delivery in hand still
    finishes and the rest of its batch goes back on the queue.
    """
    logger.error("stopping the process so it can be restarted")
    signal.raise_signal(signal.SIGTERM)


def report_exit(
    what: str, shutdown: Shutdown, halt: Callable[[], None] | None = None
) -> Callable[[asyncio.Task[None]], None]:
    """Say why a background task stopped, when nobody asked it to.

    A clean shutdown ends these tasks exactly as a failure does, so a plain stop is quiet once one
    has been asked for. `halt` is for a task the process cannot work without: a container restart
    policy watches the exit code and never the health state, so only exiting earns a restart.
    """

    def report(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("the %s stopped: %s", what, why(error), exc_info=error)
        elif not shutdown.asked:
            logger.warning("the %s stopped without an error", what)
        else:
            return
        if halt is not None:
            halt()

    return report


async def safely(what: str, closing: Awaitable[None]) -> None:
    """Run one shutdown step, reporting a failure rather than raising it."""
    try:
        await closing
    except Exception as error:
        logger.error("could not %s while shutting down: %s", what, error)


async def stop(task: asyncio.Task[None] | None, *, grace: float = 0.0) -> None:
    """Wait `grace` seconds for a task to finish on its own, then cancel it.

    Watches the task rather than awaiting its result: awaiting adopts the exception of a task that
    has already died, which would raise out of the first shutdown step and leave the rest unclosed.
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
