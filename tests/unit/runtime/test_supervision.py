from __future__ import annotations

import asyncio

import pytest

from shannon.runtime.supervision import Shutdown, report_exit, safely, stop


async def forever() -> None:
    await asyncio.sleep(3600)


async def fails() -> None:
    raise RuntimeError("the Discord bot stopped before it ever connected")


class TestStoppingBackgroundWork:
    async def test_a_task_that_already_died_does_not_raise(self) -> None:
        """It used to, and that skipped every cleanup step after it in the shutdown path."""
        task = asyncio.create_task(fails())
        await asyncio.wait({task})

        await stop(task, grace=1.0)
        await stop(task)

    async def test_a_running_task_is_cancelled_once_its_grace_runs_out(self) -> None:
        task = asyncio.create_task(forever())

        await stop(task, grace=0.01)

        assert task.cancelled()

    async def test_nothing_to_stop_is_fine(self) -> None:
        await stop(None, grace=1.0)


class TestClosingDown:
    async def test_a_step_that_fails_is_reported_rather_than_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def refuses() -> None:
            raise RuntimeError("the pool had already gone")

        with caplog.at_level("ERROR", logger="shannon.runtime.supervision"):
            await safely("close the container", refuses())

        assert "close the container" in caplog.text

    async def test_a_step_that_works_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        async def closes() -> None:
            return None

        with caplog.at_level("ERROR", logger="shannon.runtime.supervision"):
            await safely("close the container", closes())

        assert caplog.text == ""


class TestSayingWhyBackgroundWorkEnded:
    """`the delivery worker stopped without an error` means the process is now useless.

    It has to be rare, or nobody reads it. A clean shutdown ends these tasks exactly the way a
    failure does, so without knowing whether the stop was asked for it fired on every normal
    shutdown and meant nothing.
    """

    async def _run(self, coro, shutdown: Shutdown, what: str = "delivery worker") -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(report_exit(what, shutdown))
        await asyncio.wait({task})
        # The callback runs on the next loop iteration, not at completion.
        await asyncio.sleep(0)

    async def test_stopping_on_its_own_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        async def returns() -> None:
            return None

        with caplog.at_level("WARNING", logger="shannon.runtime.supervision"):
            await self._run(returns(), Shutdown())

        assert "stopped without an error" in caplog.text

    async def test_stopping_because_it_was_asked_to_is_not(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def returns() -> None:
            return None

        with caplog.at_level("WARNING", logger="shannon.runtime.supervision"):
            await self._run(returns(), Shutdown(asked=True))

        assert caplog.text == "", "every clean shutdown cried wolf about the worker dying"

    async def test_an_error_while_shutting_down_is_still_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Being on the way out is a reason to expect the exit, not to stop reading the error."""
        with caplog.at_level("ERROR", logger="shannon.runtime.supervision"):
            await self._run(fails(), Shutdown(asked=True))

        assert "the delivery worker stopped" in caplog.text

    async def test_a_cancelled_task_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        task = asyncio.create_task(forever())
        task.add_done_callback(report_exit("delivery worker", Shutdown()))
        task.cancel()

        with caplog.at_level("WARNING", logger="shannon.runtime.supervision"):
            await asyncio.wait({task})
            await asyncio.sleep(0)

        assert caplog.text == ""
