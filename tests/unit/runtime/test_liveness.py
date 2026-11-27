from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from shannon.runtime.liveness import ProcessLiveness
from shannon.runtime.supervision import ProcessTasks
from tests.support.waiting import until


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

    def connect(self) -> _Connection:
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

    async def execute(self, statement: object, /) -> None:
        return None


class Clock:
    """Moved by hand, and starting at zero: the time a clock shows never means "no probe yet"."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def forever() -> None:
    await asyncio.sleep(3600)


async def fails() -> None:
    raise RuntimeError("the Discord bot stopped before it ever connected")


async def died() -> asyncio.Task[None]:
    task = asyncio.create_task(fails())
    await asyncio.wait({task})
    return task


def alive() -> asyncio.Task[None]:
    return asyncio.create_task(forever())


def process(
    *,
    worker: asyncio.Task[None] | None = None,
    bot: asyncio.Task[None] | None = None,
    poller: asyncio.Task[None] | None = None,
    flusher: asyncio.Task[None] | None = None,
    reminders: asyncio.Task[None] | None = None,
) -> ProcessTasks:
    """The two loops every process runs, and of the rest only what a test names."""
    return ProcessTasks(
        worker_task=worker or alive(),
        bot_task=bot,
        poller_task=poller,
        flusher_task=flusher or alive(),
        reminders_task=reminders,
    )


def reporting_on(
    tasks: ProcessTasks, gateway_is_up: Callable[[], bool] = lambda: True
) -> ProcessLiveness:
    return ProcessLiveness(FakeEngine(), tasks, gateway_is_up)


def probing(
    engine: FakeEngine, *, probe_timeout: float = 5.0, clock: Callable[[], float] | None = None
) -> ProcessLiveness:
    """The window left at its default, which is the five seconds the README promises."""
    return ProcessLiveness(
        engine,
        process(),
        lambda: True,
        probe_timeout=probe_timeout,
        clock=clock if clock is not None else Clock(),
    )


class TestWhatTheProcessSaysAboutItself:
    async def test_a_working_database_is_reachable(self) -> None:
        assert await probing(FakeEngine()).database_reachable() is True

    async def test_a_database_that_refuses_is_not(self) -> None:
        assert await probing(FakeEngine(working=False)).database_reachable() is False

    async def test_the_probe_is_reused_within_its_window(self) -> None:
        """The route is public: a connection per request is what a flood would cost the server."""
        engine, clock = FakeEngine(), Clock()
        liveness = probing(engine, clock=clock)

        for _ in range(20):
            await liveness.database_reachable()
        clock.now = 4.99
        await liveness.database_reachable()

        assert engine.connections == 1

    async def test_the_probe_runs_again_once_the_window_passes(self) -> None:
        engine, clock = FakeEngine(), Clock()
        liveness = probing(engine, clock=clock)

        await liveness.database_reachable()
        clock.now = 5.0
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
        liveness = probing(engine)

        answers = await asyncio.gather(*(liveness.database_reachable() for _ in range(20)))

        assert set(answers) == {True}
        assert engine.connections == 1, "a burst opened a connection each"

    async def test_a_database_that_never_answers_does_not_park_the_endpoint(self) -> None:
        """Accepting the socket and then going quiet is a real failure and a slow one.

        Unbounded, the probe waits as long as the kernel allows and every health check queues
        behind it, so the endpoint stops answering exactly when someone needs to know why.
        """
        liveness = probing(FakeEngine(connecting=3600), probe_timeout=0.05)

        assert await asyncio.wait_for(liveness.database_reachable(), timeout=10) is False

    async def test_a_database_that_comes_back_is_noticed(self) -> None:
        engine, clock = FakeEngine(working=False), Clock()
        liveness = probing(engine, clock=clock)
        assert await liveness.database_reachable() is False

        engine.working = True
        clock.now = 5.0

        assert await liveness.database_reachable() is True

    async def test_a_probe_given_up_on_leaves_no_answer_and_holds_nothing(self) -> None:
        """A client that hangs up cancels the request it was asking in."""
        engine = FakeEngine(connecting=3600)
        liveness = probing(engine)
        asking = asyncio.create_task(liveness.database_reachable())
        await until(lambda: engine.connections == 1)

        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        engine.connecting = 0.0
        assert await asyncio.wait_for(liveness.database_reachable(), timeout=10) is True
        assert engine.connections == 2


class TestWhetherTheBackgroundWorkIsAlive:
    async def test_a_running_worker_counts_as_running(self) -> None:
        assert reporting_on(process(worker=alive())).worker_running() is True

    async def test_a_worker_that_died_is_not_running(self) -> None:
        assert reporting_on(process(worker=await died())).worker_running() is False

    async def test_polling_switched_off_is_not_something_stopped(self) -> None:
        """SHANNON_POLL_BOARDS is off, so there is no task. On is the default."""
        assert reporting_on(process(poller=None)).poller_running() is True

    async def test_a_board_still_being_read_is_running(self) -> None:
        assert reporting_on(process(poller=alive())).poller_running() is True

    async def test_a_board_that_stopped_being_read_is_reported(self) -> None:
        """Nothing is wired to stop the process when the poller dies, so without this it goes with
        a line in the log and every check afterwards says all is well.
        """
        assert reporting_on(process(poller=await died())).poller_running() is False

    async def test_transcripts_still_being_published_is_running(self) -> None:
        assert reporting_on(process(flusher=alive())).flusher_running() is True

    async def test_a_flusher_that_died_is_reported(self) -> None:
        """Nothing halts the process for this one either, so without this it dies with one line in
        the log while what people said in a logged thread piles up in a table and reaches GitHub
        never. Issue #103."""
        assert reporting_on(process(flusher=await died())).flusher_running() is False

    async def test_a_process_with_no_gateway_has_no_sender_to_have_stopped(self) -> None:
        """No token, so nowhere to send a reminder and no task started. Issue #229."""
        assert reporting_on(process(reminders=None)).reminders_running() is True

    async def test_reminders_still_being_sent_is_running(self) -> None:
        assert reporting_on(process(reminders=alive())).reminders_running() is True

    async def test_a_sender_that_died_is_reported(self) -> None:
        """Nor for this one, so without this it dies with one line in the log and every reminder
        after it is owed and never sent."""
        assert reporting_on(process(reminders=await died())).reminders_running() is False

    async def test_no_bot_configured_counts_as_connected(self) -> None:
        """Running without a token is deliberate, not a failure, whatever the client would say."""
        assert reporting_on(process(bot=None), lambda: False).bot_connected() is True

    async def test_a_bot_whose_gateway_is_up_is_connected(self) -> None:
        assert reporting_on(process(bot=alive()), lambda: True).bot_connected() is True

    async def test_a_bot_whose_gateway_is_down_is_not(self) -> None:
        """discord.py reconnects for ever, so the task is alive all through an outage."""
        assert reporting_on(process(bot=alive()), lambda: False).bot_connected() is False

    async def test_a_gateway_that_died_is_not_connected(self) -> None:
        """The worker waits for the bot once. A gateway that dies later leaves it leasing.

        Even where the client still says it is up: what it last heard is all it has to go on."""
        assert reporting_on(process(bot=await died()), lambda: True).bot_connected() is False


class TestSayingWhyItIsUnreachable:
    """The health log is the only place a runtime outage is explained.

    Nothing else writes a reason: the endpoint's own line reports `database=False` and no more,
    and there is no traceback, so a colon with nothing after it leaves the owner unable to tell a
    black-holed route from a dead server. That is exactly the case the probe deadline was added
    for, and the deadline is the one failure whose exception carries no message: asyncio raises
    `TimeoutError` with no arguments, so `str` of it is empty.
    """

    async def test_a_deadline_is_named_rather_than_left_blank(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        liveness = probing(FakeEngine(connecting=3600), probe_timeout=0.01)

        with caplog.at_level("WARNING"):
            assert await liveness.database_reachable() is False

        assert "not reachable: TimeoutError" in caplog.text

    async def test_an_ordinary_refusal_still_says_what_it_said(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            assert await probing(FakeEngine(working=False)).database_reachable() is False

        assert "the database is not there" in caplog.text
