"""The order things start and stop in, and what a task dying does to the process. Issue #272.

The integration tests run the real container against a real database. Here every part is a
stand-in that writes what happened to it into one list, so an order can be read off that list and
no test has to guess at a sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import cast

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shannon.config import Settings
from shannon.runtime import lifespan as lifespan_module
from shannon.runtime.lifespan import Gateway, build_lifespan
from shannon.runtime.liveness import ProcessLiveness
from shannon.services.verification import GitHubIdentityVerification
from tests.support.waiting import until

# Never connected to: building an engine opens nothing, and the startup check is stood in for.
NOWHERE = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing"

LOOPS = ("worker", "poller", "flusher", "reminders")

Ready = Callable[[], Awaitable[None]]


class RecordingLoop:
    """A long-lived loop: it runs until it is asked to stop, or until a test ends it."""

    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.running = False
        # What `run_forever` raises on its way out, once it has been told to end.
        self.raises: Exception | None = None
        # How many turns of the event loop its last piece of work takes. Turns and not seconds, so
        # a slow one costs no time and is slow by the same amount on every machine.
        self.turns_to_finish = 0
        self._over = asyncio.Event()

    def stop(self) -> None:
        self.events.append(f"{self.name} asked to stop")
        self._over.set()

    def dies(self, error: Exception | None) -> None:
        """End on its own, the way a loop nobody asked to stop does."""
        self.raises = error
        self._over.set()

    async def run(self, wait_for_ready: Ready | None) -> None:
        self.events.append(f"{self.name} started")
        if wait_for_ready is not None:
            await wait_for_ready()
        self.running = True
        await self._over.wait()
        for _ in range(self.turns_to_finish):
            await asyncio.sleep(0)
        self.running = False
        self.events.append(f"{self.name} ended")
        if self.raises is not None:
            raise self.raises


class RecordingWorker(RecordingLoop):
    async def run_forever(self, wait_for_ready: Ready | None = None) -> None:
        await self.run(wait_for_ready)


class RecordingSender(RecordingLoop):
    async def run_forever(self, wait_for_ready: Ready) -> None:
        await self.run(wait_for_ready)


class RecordingFlusher(RecordingLoop):
    async def run_forever(self) -> None:
        await self.run(None)


class RecordingPoller(RecordingFlusher):
    enabled = True


class RecordingConversations:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def reload(self) -> None:
        self.events.append("conversations reloading")
        # A reload is a query, so it gives the loop away. Without this a bot started before the
        # reload had finished would look the same as one started after it.
        await asyncio.sleep(0)
        self.events.append("conversations reloaded")


class RecordingBot:
    """Connects as soon as it is started, and runs until it is closed, as the real client does."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_raises: Exception | None = None
        self._error: Exception | None = None
        self._ready = asyncio.Event()
        self._over = asyncio.Event()

    async def start(self, token: str) -> None:
        self.events.append("bot started")
        self._ready.set()
        await self._over.wait()
        if self._error is not None:
            raise self._error

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def gateway_is_up(self) -> bool:
        return self._ready.is_set()

    def dies(self, error: Exception | None) -> None:
        self._error = error
        self._over.set()

    async def close(self) -> None:
        self.events.append("bot closed")
        if self.close_raises is not None:
            raise self.close_raises
        self._over.set()


class RecordingProbe(AsyncEngine):
    """A real engine, so that whatever the health probe asks of one is there."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(create_async_engine(NOWHERE).sync_engine)
        self.events = events

    async def dispose(self, close: bool = True) -> None:
        self.events.append("probe engine disposed")


class RecordingContainer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.engine = create_async_engine(NOWHERE)
        self.worker = RecordingWorker("worker", events)
        self.poller = RecordingPoller("poller", events)
        self.flusher = RecordingFlusher("flusher", events)
        self.reminders = RecordingSender("reminders", events)
        self.conversations = RecordingConversations(events)
        self.verification: GitHubIdentityVerification | None = None

    async def aclose(self) -> None:
        self.events.append("container closed")


class Process:
    """Every stand-in one run of the lifespan needs, and what happened to them in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.bot = RecordingBot(self.events)
        self.container = RecordingContainer(self.events)
        self.probe = RecordingProbe(self.events)
        self.halts = 0
        self.app = FastAPI()

    def halt(self) -> None:
        self.halts += 1

    @property
    def loops(self) -> dict[str, RecordingLoop]:
        return {
            "worker": self.container.worker,
            "poller": self.container.poller,
            "flusher": self.container.flusher,
            "reminders": self.container.reminders,
        }

    @property
    def liveness(self) -> ProcessLiveness:
        liveness = self.app.state.liveness
        assert isinstance(liveness, ProcessLiveness)
        return liveness

    def lifespan(self, token: str = "a-token") -> AbstractAsyncContextManager[None]:
        settings = Settings(github_webhook_secret=SecretStr("x"), discord_token=SecretStr(token))
        # Cast, because the stand-in offers only what starting and stopping calls, and the Protocol
        # named `is_ready` too until #272 removed it.
        bot = cast(Gateway, self.bot)
        return build_lifespan(bot, self.container, settings, self.halt)(self.app)

    async def all_running(self) -> None:
        await until(lambda: all(loop.running for loop in self.loops.values()))

    def position(self, event: str) -> int:
        """Where `event` came. Exactly once, so that a second one cannot hide an order."""
        assert self.events.count(event) == 1, f"{event!r} in {self.events}"
        return self.events.index(event)


@pytest.fixture
def process(monkeypatch: pytest.MonkeyPatch) -> Process:
    process = Process()

    async def answers(engine: AsyncEngine) -> None:
        return None

    def probe_for(engine: AsyncEngine) -> AsyncEngine:
        assert engine is process.container.engine
        return process.probe

    monkeypatch.setattr(lifespan_module, "require_database", answers)
    monkeypatch.setattr(lifespan_module, "build_probe_engine", probe_for)
    return process


class TestStartingUp:
    async def test_conversations_are_reloaded_before_the_bot_starts(self, process: Process) -> None:
        """A message cannot arrive before the gateway is up, so a set filled before it is whole."""
        async with process.lifespan():
            await process.all_running()

        assert process.position("conversations reloaded") < process.position("bot started")

    async def test_every_loop_is_started_and_reported_as_running(self, process: Process) -> None:
        async with process.lifespan():
            await process.all_running()

            assert process.liveness.worker_running() is True
            assert process.liveness.poller_running() is True
            assert process.liveness.flusher_running() is True
            assert process.liveness.reminders_running() is True
            assert process.liveness.bot_connected() is True

    async def test_without_a_token_no_bot_and_no_sender_start(self, process: Process) -> None:
        async with process.lifespan(token=""):
            await until(lambda: process.container.worker.running)
            await until(lambda: process.container.flusher.running)

            # Nothing to run is not the same as something that died.
            assert process.liveness.bot_connected() is True
            assert process.liveness.reminders_running() is True

        assert "bot started" not in process.events
        assert "bot closed" not in process.events, "a bot never started was closed"
        assert "reminders started" not in process.events
        # Still told to stop: shutdown has one path, not two.
        assert "reminders asked to stop" in process.events
        assert process.halts == 0

    async def test_a_disabled_poller_is_never_run_and_is_still_asked_to_stop(
        self, process: Process
    ) -> None:
        process.container.poller.enabled = False

        async with process.lifespan():
            await until(lambda: process.container.worker.running)
            await until(lambda: process.container.reminders.running)

            assert process.liveness.poller_running() is True

        assert "poller started" not in process.events
        assert "poller asked to stop" in process.events
        assert process.halts == 0


class TestShuttingDown:
    async def test_it_stops_the_loops_then_the_bot_then_the_container_then_the_probe(
        self, process: Process
    ) -> None:
        """A loop still running needs Discord and the database, so each outlives what uses it."""
        async with process.lifespan():
            await process.all_running()

        bot_closed = process.position("bot closed")
        for name in LOOPS:
            assert process.position(f"{name} asked to stop") < bot_closed
            assert process.position(f"{name} ended") < bot_closed
        assert bot_closed < process.position("container closed")
        assert process.position("container closed") < process.position("probe engine disposed")
        assert process.halts == 0

    @pytest.mark.parametrize("name", LOOPS)
    async def test_the_bot_stays_open_for_the_last_work_of_a_slow_loop(
        self, process: Process, name: str
    ) -> None:
        """Far more turns than the rest of shutdown takes, so a loop nobody waits for is seen."""
        process.loops[name].turns_to_finish = 200

        async with process.lifespan():
            await process.all_running()

        assert process.position(f"{name} ended") < process.position("bot closed")

    async def test_every_loop_is_asked_before_any_is_waited_for(self, process: Process) -> None:
        """Asked together, so that the four finish at once and not one grace after another."""
        async with process.lifespan():
            await process.all_running()

        last_asked = max(process.position(f"{name} asked to stop") for name in LOOPS)
        first_ended = min(process.position(f"{name} ended") for name in LOOPS)
        assert last_asked < first_ended

    async def test_the_flusher_is_started_stopped_and_reported(self, process: Process) -> None:
        async with process.lifespan():
            await until(lambda: process.container.flusher.running)
            assert process.liveness.flusher_running() is True
            assert "flusher asked to stop" not in process.events

        assert process.position("flusher started") < process.position("flusher asked to stop")
        assert process.position("flusher asked to stop") < process.position("flusher ended")
        assert process.liveness.flusher_running() is False

    async def test_a_bot_that_will_not_close_still_leaves_the_rest_closed(
        self, process: Process
    ) -> None:
        process.bot.close_raises = RuntimeError("the websocket had already gone")

        async with process.lifespan():
            await process.all_running()

        assert process.position("bot closed") < process.position("container closed")
        assert process.position("container closed") < process.position("probe engine disposed")
        assert process.liveness.bot_connected() is False, "the bot was left running"

    @pytest.mark.parametrize("name", LOOPS)
    async def test_a_loop_that_raises_while_stopping_does_not_stop_the_rest(
        self, process: Process, name: str
    ) -> None:
        process.loops[name].raises = RuntimeError("the pool had already gone")

        async with process.lifespan():
            await process.all_running()

        for other in LOOPS:
            assert process.position(f"{other} ended") < process.position("bot closed")
        assert process.position("bot closed") < process.position("container closed")
        assert process.position("container closed") < process.position("probe engine disposed")


class TestATaskThatDies:
    """The log line comes before the halt in one callback, so a test that has seen the line has
    also seen whether the process was asked to stop."""

    @pytest.mark.parametrize(
        ("name", "what"),
        [
            ("poller", "project poller"),
            ("flusher", "transcript flusher"),
            ("reminders", "reminder sender"),
        ],
    )
    async def test_a_loop_beside_the_worker_is_reported_and_halts_nothing(
        self, process: Process, caplog: pytest.LogCaptureFixture, name: str, what: str
    ) -> None:
        """Webhooks still mirror everything, so stopping the process over one is worse."""

        def reported() -> dict[str, bool]:
            return {
                "worker": process.liveness.worker_running(),
                "poller": process.liveness.poller_running(),
                "flusher": process.liveness.flusher_running(),
                "reminders": process.liveness.reminders_running(),
            }

        with caplog.at_level("WARNING"):
            async with process.lifespan():
                await process.all_running()
                assert reported() == dict.fromkeys(LOOPS, True)

                process.loops[name].dies(RuntimeError("it fell over"))
                await until(lambda: f"the {what} stopped: it fell over" in caplog.text)

                assert reported() == dict.fromkeys(LOOPS, True) | {name: False}
                assert process.liveness.bot_connected() is True

        assert process.halts == 0

    @pytest.mark.parametrize(
        ("error", "line"),
        [
            (RuntimeError("it fell over"), "the delivery worker stopped: it fell over"),
            (None, "the delivery worker stopped without an error"),
        ],
        ids=["raising", "returning"],
    )
    async def test_the_worker_dying_halts_exactly_once(
        self,
        process: Process,
        caplog: pytest.LogCaptureFixture,
        error: Exception | None,
        line: str,
    ) -> None:
        with caplog.at_level("WARNING"):
            async with process.lifespan():
                await process.all_running()

                process.container.worker.dies(error)
                await until(lambda: line in caplog.text)

                assert process.halts == 1
                assert process.liveness.worker_running() is False

        # Counted again, because the shutdown it asked for must not read as another death.
        assert process.halts == 1
        assert process.position("bot closed") < process.position("container closed")

    @pytest.mark.parametrize(
        ("error", "line"),
        [
            (RuntimeError("it fell over"), "the Discord bot stopped: it fell over"),
            (None, "the Discord bot stopped without an error"),
        ],
        ids=["raising", "returning"],
    )
    async def test_the_bot_dying_halts_exactly_once(
        self,
        process: Process,
        caplog: pytest.LogCaptureFixture,
        error: Exception | None,
        line: str,
    ) -> None:
        """Once it has connected. One that never does takes the worker waiting for it down too."""
        with caplog.at_level("WARNING"):
            async with process.lifespan():
                await process.all_running()

                process.bot.dies(error)
                await until(lambda: line in caplog.text)

                assert process.halts == 1
                assert process.liveness.bot_connected() is False

        assert process.halts == 1
        assert process.position("container closed") < process.position("probe engine disposed")
