"""The shutdown waits add up, and nothing in the code says what they add up to.

The limits were each written in a different file against Docker's ten second default, none aware
of the others. They run one after another, so what the container has to allow is their sum, and
Compose is the only place that can be told. Raising any of them without raising
`stop_grace_period` puts it back to being killed part way through, which strands the rest of a
leased batch for a fifteen minute lease while the replacement process polls a queue that looks
empty.

So this reads the numbers out of the code, reads the allowance out of the deployment, and holds
them to the one relationship that matters. Both compose files are held to it: production is the
one that matters, and it was not read at all once.

The sum is only worth having if it is the whole of it, and for a long time it was not: the waits
for open requests, for a cancel to be taken and for anything to close had no limit, so no sum was
(#272). The last test here runs a shutdown in which everything is stuck, and holds what it takes to
the sum, less the wait for open requests that uvicorn makes before it. A step with no limit, or one
the sum leaves out, fails there.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import selectors
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shannon.config import Settings
from shannon.main import HTTP_GRACE_SECONDS
from shannon.runtime import lifespan as lifespan_module
from shannon.runtime.lifespan import CANCEL_GRACE_SECONDS, CLOSE_SECONDS, build_lifespan
from shannon.runtime.supervision import ProcessTasks
from shannon.services.sync.threads import CLAIM_GRACE_SECONDS
from shannon.services.verification import GitHubIdentityVerification

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "compose.prod.yaml")

# `1h30s` and `500ms` are legal here too. Only seconds are used, and a unit this does not
# understand fails the match rather than being read as something it is not.
GRACE = re.compile(r"^\s*stop_grace_period:\s*(\d+)s\s*$", re.MULTILINE)

# A service of either file: its name, and everything indented under it.
SERVICE = re.compile(r"^  (\w+):\n((?:    .*\n|\s*\n)+)", re.MULTILINE)
INIT = re.compile(r"^    init: true$", re.MULTILINE)

# What is closed under a limit of its own: the Discord client, the bot's task and the container.
# A literal, and the stuck shutdown at the end is what holds it.
CLOSING_STEPS = 3

# Never connected to: building an engine opens nothing, and the startup check is stood in for.
NOWHERE = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing"


def the_settings() -> Settings:
    return Settings(github_webhook_secret=SecretStr("x"), discord_token=SecretStr("a-token"))


def waited_loops() -> list[str]:
    """Every task the shutdown asks to stop and then waits out with the shared grace.

    All of them but the bot's, which is closed rather than waited out. Read off the record itself,
    so a loop added to the process is counted the moment it can be running: the count used to be a
    literal, and the transcript flusher was waited on for weeks before anything noticed.
    """
    return [
        field.name
        for field in dataclasses.fields(ProcessTasks)
        if field.name.endswith("_task") and field.name != "bot_task"
    ]


def worst_case() -> float:
    """How long shutdown can take, adding the waits rather than assuming they overlap.

    Every loop is told to stop before any is waited on, so in practice the later waits are already
    over. In the worst case they are not, and a budget worked out from the usual case is not a
    budget. The unwind is counted once, because what is left is cancelled together.
    """
    grace = the_settings().worker_shutdown_grace_seconds
    return (
        HTTP_GRACE_SECONDS
        + grace * len(waited_loops())
        + CANCEL_GRACE_SECONDS
        + CLOSE_SECONDS * CLOSING_STEPS
    )


async def test_every_task_but_the_bots_is_waited_for() -> None:
    """The reading checked against what shutdown waits for. Were the record's names to change
    shape, the count above would quietly fall to nothing and every budget would pass; and a loop
    added to the record and left out of `loops` would be waited for by nobody."""
    tasks = {
        field.name: asyncio.create_task(asyncio.sleep(0))
        for field in dataclasses.fields(ProcessTasks)
        if field.name.endswith("_task")
    }
    assert waited_loops(), "no task is named the way this counts them"

    waited = ProcessTasks(**tasks).loops()

    assert len(waited) == len(set(waited))
    assert set(waited) == {tasks[name] for name in waited_loops()}


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_the_container_is_given_longer_than_the_process_can_take(compose: Path) -> None:
    """The app's own block, so that a period set on another service would not pass for it."""
    services = dict(SERVICE.findall(compose.read_text(encoding="utf-8")))
    allowed = [int(seconds) for seconds in GRACE.findall(services["app"])]

    assert allowed, f"{compose.name} gives the app no stop_grace_period, so Docker allows ten"
    assert min(allowed) > worst_case(), (
        f"shutdown can take {worst_case()}s and {compose.name} has the container killed after "
        f"{min(allowed)}s"
    )


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_the_app_is_not_the_containers_first_process(compose: Path) -> None:
    """As PID 1 it outlives its own halt, for as long as any task shutdown left behind does (#272).

    The app's own block is read, so that the same line on the database would not pass for it.
    """
    services = dict(SERVICE.findall(compose.read_text(encoding="utf-8")))

    assert INIT.search(services["app"]), f"{compose.name} does not give the app an init"


def test_a_cancelled_loop_outlasts_the_claim_it_may_be_waiting_for() -> None:
    """A thread being opened gets `CLAIM_GRACE_SECONDS` to be attached to its item. Shutdown giving
    up first would abandon it with the thread in the channel and nothing pointing at it."""
    assert CANCEL_GRACE_SECONDS > CLAIM_GRACE_SECONDS


class SkippingSelector(selectors.DefaultSelector):
    """Moves the clock on instead of waiting, so a wait costs its own length and no time at all."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is not None and timeout > 0:
            self.now += timeout
            timeout = 0
        return super().select(timeout)


class SkippingLoop(asyncio.SelectorEventLoop):
    """An event loop on that clock: exact, so the shutdown's real limits can be added up."""

    def __init__(self) -> None:
        self.skipping = SkippingSelector()
        super().__init__(self.skipping)

    def time(self) -> float:
        return self.skipping.now


Ready = Callable[[], Awaitable[None]]


class Stuck:
    """Every part of the process at once, none of which stops, closes, or takes a cancel."""

    enabled = True
    verification: GitHubIdentityVerification | None = None

    def __init__(self) -> None:
        self.let_go = asyncio.Event()
        self.engine = create_async_engine(NOWHERE)

    @property
    def worker(self) -> Stuck:
        return self

    poller = flusher = reminders = conversations = worker

    async def _never(self) -> None:
        while not self.let_go.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.let_go.wait()

    async def run_forever(self, wait_for_ready: Ready | None = None) -> None:
        await self._never()

    async def start(self, token: str) -> None:
        await self._never()

    async def close(self) -> None:
        await self._never()

    async def aclose(self) -> None:
        await self._never()

    async def wait_until_ready(self) -> None:
        await self._never()

    async def reload(self) -> None:
        return None

    def gateway_is_up(self) -> bool:
        return False

    def stop(self) -> None:
        return None


async def shut_down_with_everything_stuck() -> float:
    """How long the lifespan's own shutdown takes, on the loop's clock."""
    clock = asyncio.get_running_loop().time
    process = Stuck()
    lifespan = build_lifespan(process, process, the_settings(), lambda: None)(FastAPI())

    await lifespan.__aenter__()
    # Every task into its own wait first, so that a cancel reaches the inside of it.
    await asyncio.sleep(0)
    started = clock()
    closing = asyncio.ensure_future(lifespan.__aexit__(None, None, None))
    # An hour is far past any limit. Watched and not bounded, because what a step with no limit is
    # waiting for takes the cancel and stays.
    await asyncio.wait({closing}, timeout=3600)
    took = clock() - started

    # Or closing the event loop would wait for what was left behind.
    process.let_go.set()
    left = asyncio.all_tasks() - {asyncio.current_task()}
    assert left, "nothing was left running, so nothing was stuck"
    await asyncio.wait(left)
    await closing
    return took


def test_a_shutdown_with_everything_stuck_takes_the_budget_and_no_longer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From both sides. Longer, and there is a step the budget does not count; shorter, and the
    budget counts one that is not there, which is how a sum stops being read.

    Uvicorn's wait for open requests comes before the lifespan's shutdown and is not in this.
    """

    async def answers(engine: AsyncEngine) -> None:
        return None

    monkeypatch.setattr(lifespan_module, "require_database", answers)

    with asyncio.Runner(loop_factory=SkippingLoop) as runner:
        took = runner.run(shut_down_with_everything_stuck())

    assert took == pytest.approx(worst_case() - HTTP_GRACE_SECONDS, abs=0.5)
