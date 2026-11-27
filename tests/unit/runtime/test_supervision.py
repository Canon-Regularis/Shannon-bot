from __future__ import annotations

import asyncio
import gc
import logging
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

import pytest

from shannon.runtime.supervision import (
    ProcessTasks,
    Shutdown,
    cancel,
    report_exit,
    safely,
    stop,
    watch,
)
from tests.support.waiting import until

# A wait that must never be waited out: far above what any test here takes.
NEVER = 30.0

SUPERVISION = "shannon.runtime.supervision"


async def forever() -> None:
    await asyncio.sleep(3600)


async def returns() -> None:
    return None


async def fails() -> None:
    raise RuntimeError("the Discord bot stopped before it ever connected")


class Stubborn:
    """Work that takes every cancel and carries on, until the test lets it go."""

    def __init__(self) -> None:
        self.cancels = 0
        self.let_go = asyncio.Event()

    async def run(self) -> None:
        while not self.let_go.is_set():
            try:
                await self.let_go.wait()
            except asyncio.CancelledError:
                self.cancels += 1


@pytest.fixture
async def stubborn() -> AsyncGenerator[Stubborn]:
    """Let go of at the end, or closing the event loop would wait for it and never return."""
    work = Stubborn()
    yield work
    work.let_go.set()


def lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in caplog.records]


async def running(name: str, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """A named task that has got as far as its first wait, so a cancel reaches the inside of it."""
    task = asyncio.create_task(work, name=name)
    await asyncio.sleep(0)
    return task


class TestStoppingTheLoops:
    async def test_a_task_that_already_died_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """It used to, and that skipped every cleanup step after it in the shutdown path."""
        task = await running("delivery worker", fails())
        await asyncio.wait({task})

        await stop([task], grace=1.0, cancel_within=1.0)
        await stop([task], grace=0.0, cancel_within=1.0)

        assert lines(caplog) == []

    async def test_nothing_to_stop_is_fine(self) -> None:
        await stop([], grace=1.0, cancel_within=1.0)

    async def test_a_running_loop_is_named_and_cancelled_once_its_grace_runs_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = await running("project poller", forever())

        await stop([task], grace=0.01, cancel_within=NEVER)

        assert task.cancelled()
        assert lines(caplog) == [
            (logging.WARNING, "the project poller did not stop when asked, cancelling it")
        ]

    async def test_no_grace_at_all_cancels_at_once_and_says_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Settings allow a grace of 0, and nothing given no time has failed to stop in it."""
        task = await running("delivery worker", forever())

        await stop([task], grace=0.0, cancel_within=NEVER)

        assert task.cancelled()
        assert lines(caplog) == []

    async def test_no_loop_is_cancelled_before_every_grace_is_over(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The first has overrun its grace while the second still has half of its own."""
        grace = 0.1
        events: list[str] = []

        async def never_ends() -> None:
            try:
                await forever()
            finally:
                events.append("the worker was cancelled")

        async def ends_during_its_own_grace() -> None:
            await asyncio.sleep(grace * 1.5)
            events.append("the poller ended")

        worker = await running("delivery worker", never_ends())
        poller = await running("project poller", ends_during_its_own_grace())

        await stop([worker, poller], grace=grace, cancel_within=NEVER)

        assert events == ["the poller ended", "the worker was cancelled"]
        assert not poller.cancelled()

    async def test_a_loop_that_ends_during_a_later_grace_is_not_cancelled_or_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        grace = 0.1

        async def ends_during_the_next_ones_grace() -> None:
            await asyncio.sleep(grace * 1.5)

        worker = await running("delivery worker", ends_during_the_next_ones_grace())
        poller = await running("project poller", forever())

        await stop([worker, poller], grace=grace, cancel_within=NEVER)

        assert not worker.cancelled()
        assert lines(caplog) == [
            (logging.WARNING, "the project poller did not stop when asked, cancelling it")
        ]

    async def test_the_loops_left_are_cancelled_together(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """So two slow unwinds take one unwind's time. Each of these ends once it has seen the
        other cancelled, which a second cancel that waits for the first unwind never gives it."""
        seen: dict[str, asyncio.Event] = {"worker": asyncio.Event(), "poller": asyncio.Event()}

        async def unwinds_with(other: str, own: str) -> None:
            try:
                await forever()
            finally:
                seen[own].set()
                await seen[other].wait()

        worker = await running("delivery worker", unwinds_with("poller", "worker"))
        poller = await running("project poller", unwinds_with("worker", "poller"))

        await stop([worker, poller], grace=0.0, cancel_within=5.0)

        assert worker.cancelled()
        assert poller.cancelled()
        assert lines(caplog) == []

    async def test_a_loop_that_outlives_its_cancel_is_named_and_left(
        self, stubborn: Stubborn, caplog: pytest.LogCaptureFixture
    ) -> None:
        worker = await running("delivery worker", stubborn.run())

        await stop([worker], grace=0.01, cancel_within=0.02)

        assert not worker.done()
        assert lines(caplog) == [
            (logging.WARNING, "the delivery worker did not stop when asked, cancelling it"),
            (
                logging.ERROR,
                "the delivery worker is still running 0.02s after being cancelled, leaving it",
            ),
        ]
        assert stubborn.cancels == 1, "a second cancel cuts into the hand-back the first started"

    async def test_a_loop_that_raises_while_it_unwinds_does_not_raise_here(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def raises_on_the_way_out() -> None:
            try:
                await forever()
            finally:
                raise RuntimeError("the pool had already gone")

        worker = watch("delivery worker", raises_on_the_way_out(), Shutdown(asked=True))
        await asyncio.sleep(0)

        await stop([worker], grace=0.0, cancel_within=NEVER)

        assert worker.done()
        await until(lambda: lines(caplog) != [])
        assert lines(caplog) == [
            (logging.ERROR, "the delivery worker stopped: the pool had already gone")
        ]

    async def test_cancelling_whoever_is_waiting_is_passed_on(self) -> None:
        """The loops are left to whoever did it, which cancels every task it finds."""
        worker = await running("delivery worker", forever())
        stopping = await running("shutdown", stop([worker], grace=NEVER, cancel_within=NEVER))

        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

        assert not worker.done()
        worker.cancel()


class TestCancelling:
    async def test_a_task_still_running_is_cancelled_without_a_word(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """How the bot's task is ended: its client has been closed, so there is nothing to say."""
        bot = watch("Discord bot", forever(), Shutdown(asked=True))
        await asyncio.sleep(0)

        await cancel([bot], within=NEVER)

        assert bot.cancelled()
        assert lines(caplog) == []

    async def test_a_task_that_has_already_ended_is_left_alone(self) -> None:
        bot = await running("Discord bot", returns())

        await cancel([bot], within=NEVER)

        assert not bot.cancelled()

    async def test_nothing_to_cancel_is_fine(self) -> None:
        await cancel([], within=NEVER)

    async def test_each_one_still_running_is_named(
        self, stubborn: Stubborn, caplog: pytest.LogCaptureFixture
    ) -> None:
        worker = await running("delivery worker", stubborn.run())
        poller = await running("project poller", forever())
        flusher = await running("transcript flusher", stubborn.run())

        await cancel([worker, poller, flusher], within=0.02)

        assert poller.cancelled()
        assert lines(caplog) == [
            (
                logging.ERROR,
                "the delivery worker is still running 0.02s after being cancelled, leaving it",
            ),
            (
                logging.ERROR,
                "the transcript flusher is still running 0.02s after being cancelled, leaving it",
            ),
        ]


class TestClosingDown:
    async def test_a_step_that_fails_is_reported_rather_than_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def refuses() -> None:
            raise RuntimeError("the pool had already gone")

        await safely("close the container", refuses(), within=NEVER)

        assert lines(caplog) == [
            (
                logging.ERROR,
                "could not close the container while shutting down: the pool had already gone",
            )
        ]

    async def test_an_error_with_no_words_is_named(self, caplog: pytest.LogCaptureFixture) -> None:
        """`str(TimeoutError())` is empty, so the line used to end in a colon and nothing."""

        async def times_out() -> None:
            raise TimeoutError

        await safely("close the container", times_out(), within=NEVER)

        assert lines(caplog) == [
            (logging.ERROR, "could not close the container while shutting down: TimeoutError")
        ]

    async def test_a_step_that_works_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        await safely("close the container", returns(), within=NEVER)

        assert lines(caplog) == []

    async def test_a_step_that_never_returns_is_cancelled_and_named(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cancelled = asyncio.Event()

        async def hangs() -> None:
            try:
                await forever()
            finally:
                cancelled.set()

        await safely("close the Discord client", hangs(), within=0.02)

        assert lines(caplog) == [
            (
                logging.ERROR,
                "could not close the Discord client while shutting down: "
                "still not done after 0.02s",
            )
        ]
        async with asyncio.timeout(NEVER):
            await cancelled.wait()

    async def test_a_step_that_will_not_be_cancelled_is_not_waited_for(
        self, stubborn: Stubborn, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What a deadline around an await does not give: it waits for the cancel to be taken."""
        async with asyncio.timeout(NEVER):
            await safely("close the container", stubborn.run(), within=0.02)

        assert [level for level, _ in lines(caplog)] == [logging.ERROR]
        await until(lambda: stubborn.cancels > 0)
        assert stubborn.cancels == 1

    async def test_a_step_that_ends_cancelled_is_one_step_that_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """It used to pass for the shutdown itself being cancelled, and the rest was skipped."""

        async def cancelled_from_inside() -> None:
            raise asyncio.CancelledError

        await safely("close the Discord client", cancelled_from_inside(), within=NEVER)

        assert lines(caplog) == [
            (
                logging.ERROR,
                "could not close the Discord client while shutting down: it was cancelled",
            )
        ]

    async def test_an_abandoned_step_that_fails_later_is_not_reported_twice(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """asyncio's own words for it, "exception was never retrieved", name a different fault."""
        ended = asyncio.Event()

        async def fails_once_cancelled() -> None:
            try:
                await forever()
            finally:
                ended.set()
                raise RuntimeError("the connection was cut")

        await safely("close the container", fails_once_cancelled(), within=0.02)
        async with asyncio.timeout(NEVER):
            await ended.wait()
        # The callback that reads it runs on the turn after, and the report comes from collecting.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()

        assert [message for _, message in lines(caplog)] == [
            "could not close the container while shutting down: still not done after 0.02s"
        ]

    async def test_cancelling_whoever_is_waiting_cancels_the_step_and_is_passed_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cancelled = asyncio.Event()

        async def hangs() -> None:
            try:
                await forever()
            finally:
                cancelled.set()

        shutdown = await running("shutdown", safely("close", hangs(), within=NEVER))
        await asyncio.sleep(0)

        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown

        async with asyncio.timeout(NEVER):
            await cancelled.wait()
        assert lines(caplog) == []


class TestTheListOfTasks:
    async def test_the_loops_come_in_the_order_shutdown_waits_for_them(self) -> None:
        worker, bot, poller, flusher, reminders = [
            await running(name, returns())
            for name in ("worker", "bot", "poller", "flusher", "reminders")
        ]
        tasks = ProcessTasks(
            worker_task=worker,
            bot_task=bot,
            poller_task=poller,
            flusher_task=flusher,
            reminders_task=reminders,
        )

        assert tasks.loops() == (worker, poller, flusher, reminders)

    async def test_a_loop_this_process_does_not_run_is_left_out(self) -> None:
        worker, flusher = [await running(name, returns()) for name in ("worker", "flusher")]
        tasks = ProcessTasks(
            worker_task=worker,
            bot_task=None,
            poller_task=None,
            flusher_task=flusher,
            reminders_task=None,
        )

        assert tasks.loops() == (worker, flusher)


class TestWatching:
    async def test_the_task_carries_the_name_its_log_line_uses(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = watch("delivery worker", fails(), Shutdown())
        await until(lambda: lines(caplog) != [])

        assert task.get_name() == "delivery worker"
        assert lines(caplog) == [
            (
                logging.ERROR,
                "the delivery worker stopped: the Discord bot stopped before it ever connected",
            )
        ]

    @pytest.mark.parametrize("work", [fails, returns], ids=["raising", "returning"])
    async def test_a_task_that_ends_by_itself_halts_once(self, work: Any) -> None:
        halts: list[str] = []
        task = watch("delivery worker", work(), Shutdown(), lambda: halts.append("halt"))
        await asyncio.wait({task})
        await until(lambda: halts != [])

        assert halts == ["halt"]

    async def test_a_task_that_is_cancelled_or_was_asked_to_stop_halts_nothing(self) -> None:
        halts: list[str] = []
        cancelled = watch("Discord bot", forever(), Shutdown(), lambda: halts.append("halt"))
        asked = watch("worker", returns(), Shutdown(asked=True), lambda: halts.append("halt"))
        cancelled.cancel()
        await asyncio.wait({cancelled, asked})
        await asyncio.sleep(0)

        assert halts == []


class TestSayingWhyBackgroundWorkEnded:
    """`the delivery worker stopped without an error` means the process is now useless.

    It has to be rare, or nobody reads it. A clean shutdown ends these tasks exactly the way a
    failure does, so without knowing whether the stop was asked for it fired on every normal
    shutdown and meant nothing.
    """

    async def _run(
        self, coro: Coroutine[Any, Any, None], shutdown: Shutdown, what: str = "delivery worker"
    ) -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(report_exit(what, shutdown))
        await asyncio.wait({task})
        # The callback runs on the next loop iteration, not at completion.
        await asyncio.sleep(0)

    async def test_stopping_on_its_own_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger=SUPERVISION):
            await self._run(returns(), Shutdown())

        assert "stopped without an error" in caplog.text

    async def test_stopping_because_it_was_asked_to_is_not(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger=SUPERVISION):
            await self._run(returns(), Shutdown(asked=True))

        assert caplog.text == "", "every clean shutdown cried wolf about the worker dying"

    async def test_an_error_while_shutting_down_is_still_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Being on the way out is a reason to expect the exit, not to stop reading the error."""
        with caplog.at_level("ERROR", logger=SUPERVISION):
            await self._run(fails(), Shutdown(asked=True))

        assert "the delivery worker stopped" in caplog.text

    async def test_a_cancelled_task_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        task = asyncio.create_task(forever())
        task.add_done_callback(report_exit("delivery worker", Shutdown()))
        task.cancel()

        with caplog.at_level("WARNING", logger=SUPERVISION):
            await asyncio.wait({task})
            await asyncio.sleep(0)

        assert caplog.text == ""
