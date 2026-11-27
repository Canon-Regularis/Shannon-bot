"""Starting, watching and stopping the tasks that run beside the API."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Shutdown:
    """Whether the process asked for what is about to happen.

    A background task ending is an emergency or a formality, and the task itself says neither.
    """

    asked: bool = False


@dataclass(frozen=True, slots=True)
class ProcessTasks:
    """Every task this process runs beside the API, in the one list of them there is.

    Read by the shutdown and by `/health` both, so a loop one of them knows about is never a loop
    the other has not heard of (#272). No field has a default: a task left out would read as one
    this deployment does not run, which is the answer that raises no alarm.
    """

    worker_task: asyncio.Task[None]
    # None means no token was configured (no-bot mode); a finished task means the gateway went.
    bot_task: asyncio.Task[None] | None
    # None means SHANNON_POLL_BOARDS is off, so this process does not poll; on is the default.
    # A finished task means boards are no longer read.
    poller_task: asyncio.Task[None] | None
    flusher_task: asyncio.Task[None]
    # None means no token was configured, so there is nowhere to send a reminder. Issue #229.
    reminders_task: asyncio.Task[None] | None

    def loops(self) -> tuple[asyncio.Task[None], ...]:
        """The loops this process is running, in the order shutdown waits for them.

        Not the bot, which is closed rather than asked to stop and waited for.
        """
        loops = (self.worker_task, self.poller_task, self.flusher_task, self.reminders_task)
        return tuple(loop for loop in loops if loop is not None)


def why(error: BaseException) -> str:
    """What went wrong, in words, even when the exception has none.

    `str(TimeoutError())` is the empty string, because asyncio raises it with no arguments, and an
    unanswered connection is the failure that most needs naming in the log.
    """
    return str(error) or type(error).__name__


def ask_the_process_to_stop() -> None:
    """Send this process the signal an orchestrator would send it.

    Uvicorn owns the exit, and the signal runs its ordinary shutdown: the delivery in hand still
    finishes and the rest of its batch goes back on the queue. As PID 1 the process can outlive that
    shutdown, waiting on a task it left behind, so the compose files give it an init (#272).
    """
    logger.error("stopping the process so it can be restarted")
    signal.raise_signal(signal.SIGTERM)


def report_exit(
    what: str, shutdown: Shutdown, halt: Callable[[], None] | None = None
) -> Callable[[asyncio.Task[None]], None]:
    """Say why a background task stopped, when nobody asked it to.

    A clean shutdown ends these tasks exactly as a failure does, so a plain stop is quiet once one
    has been asked for. `halt` is for a task the process cannot work without: the restart policy
    the compose files set acts on the process exiting, whatever the code, and never on the health
    state, so only exiting earns a restart.
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


def watch(
    what: str,
    work: Coroutine[Any, Any, None],
    shutdown: Shutdown,
    halt: Callable[[], None] | None = None,
) -> asyncio.Task[None]:
    """Start `work` as a task that says so when it ends.

    One call for both halves, so no task is started and left unwatched. The task carries the words
    its log line uses, and `stop` and `cancel` read them back.
    """
    task = asyncio.create_task(work, name=what)
    task.add_done_callback(report_exit(what, shutdown, halt))
    return task


async def safely(what: str, closing: Coroutine[Any, Any, None], *, within: float) -> None:
    """Run one shutdown step for at most `within` seconds, reporting a failure, never raising it.

    A task watched from outside, and not `asyncio.timeout` around an await: a timeout cancels and
    then waits for the cancel to be taken, which closing a connection to a database gone quiet does
    not do in time, so it is not a limit (#272). As a task, a step that ends in `CancelledError` is
    also one step that failed, rather than the end of every step after it.

    Cancelling the caller is another matter and is passed on. Whoever does that wants the shutdown
    over, so the step in hand is cancelled with it and the steps after it are not run.
    """
    step = asyncio.create_task(closing, name=what)
    try:
        await asyncio.wait({step}, timeout=within)
    except asyncio.CancelledError:
        _abandon(step)
        raise

    if not step.done():
        _abandon(step)
        reason = f"still not done after {within}s"
    elif step.cancelled():
        reason = "it was cancelled"
    elif (error := step.exception()) is not None:
        reason = why(error)
    else:
        return
    logger.error("could not %s while shutting down: %s", what, reason)


def _abandon(step: asyncio.Task[None]) -> None:
    """Cancel a step nobody will wait for any longer.

    What it ends with is read and dropped. Unread, it would reach the log as "Task exception was
    never retrieved" with a traceback, which looks like a second failure and is the same one.
    """
    step.add_done_callback(lambda ended: ended.cancelled() or ended.exception())
    step.cancel()


async def stop(loops: Iterable[asyncio.Task[None]], *, grace: float, cancel_within: float) -> None:
    """Wait for loops that have been asked to stop, then cancel what is left of them.

    Each is given `grace` seconds in turn, and none is cancelled until every grace is over: one
    still on its last piece of work may as well finish it while another is waited for. They are
    cancelled together, so unwinding costs the slowest one's time rather than the sum (#272). A
    `grace` of 0 cancels at once.

    Watches the tasks rather than awaiting their results: awaiting adopts the exception of a task
    that has already died, which would raise out of the first shutdown step and leave the rest
    unclosed.
    """
    asked = tuple(loops)
    if grace > 0:
        for loop in asked:
            await asyncio.wait({loop}, timeout=grace)
        for loop in asked:
            if not loop.done():
                logger.warning("the %s did not stop when asked, cancelling it", loop.get_name())
    await cancel(asked, within=cancel_within)


async def cancel(tasks: Iterable[asyncio.Task[None]], *, within: float) -> None:
    """Cancel the tasks still running and wait once, for at most `within` seconds in all.

    One that outlives it is named and left. It is never cancelled twice: a second cancel cuts into
    what it is doing about the first, which for the worker is handing its batch back.
    """
    running = [task for task in tasks if not task.done()]
    if not running:
        return

    for task in running:
        task.cancel()
    await asyncio.wait(running, timeout=within)
    for task in running:
        if not task.done():
            logger.error(
                "the %s is still running %ss after being cancelled, leaving it",
                task.get_name(),
                within,
            )
