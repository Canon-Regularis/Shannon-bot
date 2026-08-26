from __future__ import annotations

import asyncio

from shannon.runtime.liveness import ProcessLiveness


class FakeEngine:
    """An engine that counts how often anyone actually connects to it.

    `connecting` exists because a real connection is not instant, and the gap is where the
    interesting behaviour lives. An engine that answers before yielding hides every race in
    here, which is how the concurrent probes below went unnoticed.
    """

    def __init__(self, *, working: bool = True, connecting: float = 0.0) -> None:
        self.working = working
        self.connecting = connecting
        self.connections = 0

    def connect(self):
        self.connections += 1
        return _Connection(self.working, self.connecting)


class _Connection:
    def __init__(self, working: bool, connecting: float) -> None:
        self.working = working
        self.connecting = connecting

    async def __aenter__(self) -> _Connection:
        await asyncio.sleep(self.connecting)
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

    async def test_probes_landing_together_all_get_the_real_answer(self) -> None:
        """Two health checks at once is the normal case, not a rare one.

        Any orchestrator running a liveness and a readiness probe does it, as does anything
        polling while a probe is still in flight. This used to hand every caller but the first
        whatever `_reachable` happened to hold, which on the first probe is its initial False:
        a healthy process reported itself down, and got restarted for saying so.
        """
        engine = FakeEngine(connecting=0.02)
        liveness = ProcessLiveness(engine, probe_every=60.0)

        answers = await asyncio.gather(*(liveness.database_reachable() for _ in range(20)))

        assert set(answers) == {True}
        assert engine.connections == 1, "a burst opened a connection each, which starves the pool"

    async def test_a_database_that_never_answers_does_not_park_the_endpoint(self) -> None:
        """Accepting the socket and then going quiet is a real failure and a slow one.

        Unbounded, the probe waits as long as the kernel allows and every health check queues
        behind it, so the endpoint stops answering exactly when someone needs to know why.
        """
        liveness = ProcessLiveness(
            FakeEngine(connecting=3600), probe_every=60.0, probe_timeout=0.05
        )

        assert await asyncio.wait_for(liveness.database_reachable(), timeout=2) is False

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


class TestSayingWhyItIsUnreachable:
    """The health log is the only place a runtime outage is explained.

    Nothing else writes a reason: the endpoint's own line reports `database=False` and no more,
    and there is no traceback, so a colon with nothing after it leaves the owner unable to tell a
    black-holed route from a dead server. That is exactly the case the probe deadline was added
    for, and the deadline is the one failure whose exception carries no message: asyncio raises
    `TimeoutError` with no arguments, so `str` of it is empty.
    """

    async def test_a_deadline_is_named_rather_than_left_blank(self, caplog) -> None:
        liveness = ProcessLiveness(FakeEngine(connecting=1.0), probe_timeout=0.01)

        with caplog.at_level("WARNING"):
            assert await liveness.database_reachable() is False

        assert "not reachable: TimeoutError" in caplog.text

    async def test_an_ordinary_refusal_still_says_what_it_said(self, caplog) -> None:
        liveness = ProcessLiveness(FakeEngine(working=False))

        with caplog.at_level("WARNING"):
            assert await liveness.database_reachable() is False

        assert "the database is not there" in caplog.text
