from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.runtime.lifespan import build_lifespan
from shannon.services.delivery.worker import ReadyCheck
from tests.fakes.github import ClosingGitHub
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration


class FakeBot:
    """Enough of ShannonBot for the lifespan: it starts, it becomes ready, it closes."""

    def __init__(self, *, connects: bool = True) -> None:
        self.connects = connects
        self.closed = False
        self._ready = asyncio.Event()

    async def start(self, token: str) -> None:
        if not self.connects:
            raise RuntimeError("Improper token has been passed")
        self._ready.set()
        await asyncio.sleep(3600)

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    async def close(self) -> None:
        self.closed = True


class FakeWorker:
    def __init__(self, *, dies: bool = False) -> None:
        self.dies = dies
        self.stopped = False
        self.ran = asyncio.Event()

    def stop(self) -> None:
        self.stopped = True

    async def run_forever(self, wait_for_ready: ReadyCheck | None = None) -> None:
        if wait_for_ready is not None:
            await wait_for_ready()
        self.ran.set()
        if self.dies:
            raise RuntimeError("the worker fell over")
        while not self.stopped:
            await asyncio.sleep(0.01)


def container_for(engine: AsyncEngine, worker: FakeWorker, github: ClosingGitHub) -> Container:
    """The real Container, with only the worker swapped.

    A hand-rolled stand-in holding the attributes the lifespan uses today would silently stop
    covering whatever gets added tomorrow.
    """
    container = build_container(
        threads=FakeThreadGateway(),
        settings=Settings(github_webhook_secret="x"),
        engine=engine,
        github=github,
    )
    container.worker = worker
    return container


@pytest.fixture
async def migrated(db_engine: AsyncEngine) -> AsyncEngine:
    """The startup check reads alembic_version, which the test schema does not create."""
    async with db_engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32))")
        )
    return db_engine


def settings_with(token: str = "") -> Settings:
    return Settings(
        github_webhook_secret="x", discord_token=token, worker_shutdown_grace_seconds=0.05
    )


async def runbuild_lifespan(bot: Any, container: Any, settings: Settings):
    app = create_app(settings=settings)
    return app, build_lifespan(bot, container, settings)(app)


class TestStartingUp:
    async def test_a_database_it_cannot_reach_stops_startup(self) -> None:
        """Starting anyway would accept deliveries and fail every one of them behind the port.

        A refused connection surfaces as OSError rather than anything SQLAlchemy wraps, which
        is why the lifespan catches broadly and reports rather than matching a type.
        """
        engine = create_async_engine("postgresql+asyncpg://nobody:nobody@localhost:1/nothing")
        _, lifespan = await runbuild_lifespan(
            FakeBot(), container_for(engine, FakeWorker(), ClosingGitHub()), settings_with()
        )

        with pytest.raises((OSError, SQLAlchemyError)):
            async with lifespan:
                pass

        await engine.dispose()

    async def test_a_database_that_was_never_migrated_stops_startup(
        self, db_engine: AsyncEngine
    ) -> None:
        async with db_engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        _, lifespan = await runbuild_lifespan(
            FakeBot(), container_for(db_engine, FakeWorker(), ClosingGitHub()), settings_with()
        )

        with pytest.raises(SQLAlchemyError):
            async with lifespan:
                pass

    async def test_without_a_token_the_worker_still_runs(self, migrated: AsyncEngine) -> None:
        worker = FakeWorker()
        app, lifespan = await runbuild_lifespan(
            FakeBot(), container_for(migrated, worker, ClosingGitHub()), settings_with()
        )

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=2)
            assert app.state.liveness.bot_connected() is True

    async def test_with_a_token_the_worker_waits_for_the_gateway(
        self, migrated: AsyncEngine
    ) -> None:
        worker = FakeWorker()
        bot = FakeBot()
        app, lifespan = await runbuild_lifespan(
            bot, container_for(migrated, worker, ClosingGitHub()), settings_with("a-token")
        )

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=2)
            assert app.state.liveness.worker_running() is True
            assert app.state.liveness.bot_connected() is True


class TestWhileRunning:
    async def test_a_gateway_that_never_connects_is_reported_unhealthy(
        self, migrated: AsyncEngine
    ) -> None:
        worker = FakeWorker()
        app, lifespan = await runbuild_lifespan(
            FakeBot(connects=False),
            container_for(migrated, worker, ClosingGitHub()),
            settings_with("a-token"),
        )

        async with lifespan:
            await asyncio.sleep(0.2)
            assert app.state.liveness.bot_connected() is False
            assert app.state.liveness.worker_running() is False


class TestShuttingDown:
    async def test_a_clean_shutdown_says_nothing_alarming(
        self, migrated: AsyncEngine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole point of the warning is that it is rare enough to be worth reading.

        Both tasks end on a normal shutdown exactly the way they end on a failure: the worker's
        loop returns when told to stop, and the Discord client's start returns once it is closed.
        Reported either way, the line meaning the process is now useless appeared on every
        deployment restart, which is the fastest way to teach everyone to ignore it.
        """
        worker = FakeWorker()
        bot = FakeBot()
        _, lifespan = await runbuild_lifespan(
            bot, container_for(migrated, worker, ClosingGitHub()), settings_with("a-token")
        )

        with caplog.at_level("WARNING", logger="shannon.main"):
            async with lifespan:
                await asyncio.wait_for(worker.ran.wait(), timeout=2)

        assert "stopped without an error" not in caplog.text

    async def test_everything_is_closed(self, migrated: AsyncEngine) -> None:
        worker = FakeWorker()
        bot = FakeBot()
        github = ClosingGitHub()
        container = container_for(migrated, worker, github)
        _, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=2)

        assert worker.stopped is True
        assert bot.closed is True
        assert github.closed is True

    async def test_a_worker_that_already_died_does_not_stop_the_rest_closing(
        self, migrated: AsyncEngine
    ) -> None:
        """Stopping the worker is the first thing shutdown does, and it used to raise here."""
        worker = FakeWorker(dies=True)
        bot = FakeBot()
        github = ClosingGitHub()
        container = container_for(migrated, worker, github)
        _, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=2)
            await asyncio.sleep(0.05)

        assert bot.closed is True, "the Discord client was left open"
        assert github.closed is True, "the engine and HTTP client were left open"


class FakePoller:
    """A project poller with the shape the lifespan uses, and a switch for whether it runs."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.stopped = False
        self.ran = asyncio.Event()

    def stop(self) -> None:
        self.stopped = True

    async def run_forever(self) -> None:
        self.ran.set()
        while not self.stopped:
            await asyncio.sleep(0.01)


class TestTheProjectPoller:
    """Started only when a board was configured, and stopped whether it was or not.

    Zero means no board, which is the default, so most deployments never start this at all.
    Starting a task that returned at once would have the done callback report the poller as
    having stopped on every boot, for everybody not using one.
    """

    async def test_a_configured_board_is_polled(self, migrated: AsyncEngine) -> None:
        worker, poller = FakeWorker(), FakePoller(enabled=True)
        container = container_for(migrated, worker, ClosingGitHub())
        container.poller = poller

        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())
        async with lifespan:
            await asyncio.wait_for(poller.ran.wait(), timeout=5)

        assert poller.stopped is True

    async def test_no_board_means_no_task_at_all(self, migrated: AsyncEngine) -> None:
        worker, poller = FakeWorker(), FakePoller(enabled=False)
        container = container_for(migrated, worker, ClosingGitHub())
        container.poller = poller

        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())
        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)

        assert not poller.ran.is_set(), "a board nobody configured was polled anyway"
        # Still told to stop: the flag costs nothing and means shutdown has one path, not two.
        assert poller.stopped is True
