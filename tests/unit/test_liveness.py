from __future__ import annotations

import asyncio

import pytest

from shannon.main import ProcessLiveness, _safely, _stop


class FakeEngine:
    """An engine that counts how often anyone actually connects to it."""

    def __init__(self, *, working: bool = True) -> None:
        self.working = working
        self.connections = 0

    def connect(self):
        self.connections += 1
        return _Connection(self.working)


class _Connection:
    def __init__(self, working: bool) -> None:
        self.working = working

    async def __aenter__(self) -> _Connection:
        if not self.working:
            raise ConnectionRefusedError("the database is not there")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        return None


async def forever() -> None:
    await asyncio.sleep(3600)


async def fails() -> None:
    raise RuntimeError("the Discord bot stopped before it ever connected")


class TestWhatTheProcessSaysAboutItself:
    async def test_a_working_database_is_reachable(self) -> None:
        assert await ProcessLiveness(FakeEngine()).database_reachable() is True

    async def test_a_database_that_refuses_is_not(self) -> None:
        assert await ProcessLiveness(FakeEngine(working=False)).database_reachable() is False

    async def test_the_probe_is_reused_within_its_window(self) -> None:
        """The route is public: a connection per request would starve the worker's own pool."""
        engine = FakeEngine()
        liveness = ProcessLiveness(engine, probe_every=60.0)

        for _ in range(20):
            await liveness.database_reachable()

        assert engine.connections == 1

    async def test_the_probe_runs_again_once_the_window_passes(self) -> None:
        engine = FakeEngine()
        liveness = ProcessLiveness(engine, probe_every=0.01)

        await liveness.database_reachable()
        await asyncio.sleep(0.05)
        await liveness.database_reachable()

        assert engine.connections == 2

    async def test_a_database_that_comes_back_is_noticed(self) -> None:
        engine = FakeEngine(working=False)
        liveness = ProcessLiveness(engine, probe_every=0.01)
        assert await liveness.database_reachable() is False

        engine.working = True
        await asyncio.sleep(0.05)

        assert await liveness.database_reachable() is True


class TestWhetherTheBackgroundWorkIsAlive:
    async def test_a_running_worker_counts_as_running(self) -> None:
        task = asyncio.create_task(forever())
        try:
            assert ProcessLiveness(FakeEngine(), worker_task=task).worker_running() is True
        finally:
            task.cancel()

    async def test_no_worker_at_all_is_not_running(self) -> None:
        assert ProcessLiveness(FakeEngine()).worker_running() is False

    async def test_a_worker_that_died_is_not_running(self) -> None:
        task = asyncio.create_task(fails())
        await asyncio.wait({task})

        assert ProcessLiveness(FakeEngine(), worker_task=task).worker_running() is False

    async def test_no_bot_configured_counts_as_connected(self) -> None:
        """Running without a token is deliberate, not a failure."""
        assert ProcessLiveness(FakeEngine()).bot_connected() is True

    async def test_a_gateway_that_died_is_not_connected(self) -> None:
        """The worker waits for the bot once. A gateway that dies later leaves it leasing."""
        task = asyncio.create_task(fails())
        await asyncio.wait({task})

        assert ProcessLiveness(FakeEngine(), bot_task=task).bot_connected() is False


class TestStoppingBackgroundWork:
    async def test_a_task_that_already_died_does_not_raise(self) -> None:
        """It used to, and that skipped every cleanup step after it in the shutdown path."""
        task = asyncio.create_task(fails())
        await asyncio.wait({task})

        await _stop(task, grace=1.0)
        await _stop(task)

    async def test_a_running_task_is_cancelled_once_its_grace_runs_out(self) -> None:
        task = asyncio.create_task(forever())

        await _stop(task, grace=0.01)

        assert task.cancelled()

    async def test_nothing_to_stop_is_fine(self) -> None:
        await _stop(None, grace=1.0)


class TestClosingDown:
    async def test_a_step_that_fails_is_reported_rather_than_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def refuses() -> None:
            raise RuntimeError("the pool had already gone")

        with caplog.at_level("ERROR", logger="shannon.main"):
            await _safely("close the container", refuses())

        assert "close the container" in caplog.text

    async def test_a_step_that_works_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        async def closes() -> None:
            return None

        with caplog.at_level("ERROR", logger="shannon.main"):
            await _safely("close the container", closes())

        assert caplog.text == ""
