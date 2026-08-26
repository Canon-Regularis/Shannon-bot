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

    A background task ending is an emergency or a formality, and nothing about the task says
    which.
    """

    asked: bool = False


def why(error: BaseException) -> str:
    """What went wrong, in words, even when the exception has none.

    `str(TimeoutError())` is the empty string, because asyncio raises it with no arguments. That
    is the one failure here with nothing to say and the one that most needs saying: an unanswered
    connection is a dropped packet, a firewall, a security group nobody opened, and it reads in
    the log as a colon with nothing after it. Everything else, a refused connection, a wrong
    password, a database that is not there, fills its own message.
    """
    return str(error) or type(error).__name__


def ask_the_process_to_stop() -> None:
    """Send this process the signal an orchestrator would send it.

    Uvicorn owns the exit, so the way to ask for one from in here is the signal it already
    listens for. It runs the ordinary shutdown, which means the delivery in hand still finishes
    and the rest of its batch still goes back on the queue.
    """
    logger.error("stopping the process so it can be restarted")
    signal.raise_signal(signal.SIGTERM)


def report_exit(what: str, shutdown: Shutdown, halt: Callable[[], None] | None = None):
    """Say why a background task stopped, when nobody asked it to.

    A task that dies otherwise takes its exception with it while the endpoint carries on
    answering.

    Quiet once a stop has been asked for, since a clean shutdown ends these tasks exactly as a
    failure does: the worker's loop returns when told, and the Discord client's start returns
    when closed. Warning either way meant the one line that says the process is now useless
    appeared on every restart. Errors are still reported whichever way it is going.

    `halt` is for a task the process cannot do its job without. Reporting alone leaves a process
    that answers 200 to every delivery and works none of them: `/health` says so, and nothing
    reads it, because a container restart policy watches the exit code and never the health
    state. An unhealthy container that has not exited is a container that sits there until
    somebody looks. Stopping instead is what makes a restart happen, and a restart is the fix
    for a good half of what ends these tasks, a rotated token among them.

    Left off for a task the process is still useful without, which is how the poller is wired.
    """

    def report(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("the %s stopped: %s", what, error, exc_info=error)
        elif not shutdown.asked:
            logger.warning("the %s stopped without an error", what)
        else:
            return
        if halt is not None:
            halt()

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
