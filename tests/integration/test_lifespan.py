from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.container import Container, build_container
from shannon.runtime import lifespan as lifespan_module
from shannon.runtime.lifespan import build_lifespan
from shannon.services.delivery.worker import ReadyCheck
from tests.fakes.github import ClosingGitHub
from tests.fakes.threads import FakeThreadGateway
from tests.support.waiting import until

pytestmark = pytest.mark.integration


class FakeBot:
    """Enough of ShannonBot for the lifespan: it starts, it becomes ready, it closes.

    `start` returns once it is closed, as the real client's does. `close` fails the way a test
    says, and one that fails leaves `start` running.
    """

    def __init__(
        self,
        *,
        connects: bool = True,
        reaches_the_gateway: bool = True,
        close_fails: type[BaseException] | None = None,
        close_hangs: bool = False,
    ) -> None:
        self.close_fails = close_fails
        self.close_hangs = close_hangs
        self.connects = connects
        # A client that keeps trying and never arrives, which is what discord.py does on its own
        # for an outage or blocked egress: `start` runs on and the connection is never made.
        self.reaches_the_gateway = reaches_the_gateway
        self.started = False
        self.closed = False
        # How many are waiting for the gateway, so a test can tell one that has yet to be told
        # to wait from one that is waiting.
        self.waiting = 0
        self._ready = asyncio.Event()
        self._over = asyncio.Event()
        # The real client keeps this from its own connect and disconnect events, and it is what
        # the health check asks. Settable here so a test can drop the connection under it.
        self.connected = False

    async def start(self, token: str) -> None:
        self.started = True
        if not self.connects:
            raise RuntimeError("Improper token has been passed")
        if self.reaches_the_gateway:
            self._ready.set()
            self.connected = True
        await self._over.wait()

    async def wait_until_ready(self) -> None:
        self.waiting += 1
        await self._ready.wait()

    def gateway_is_up(self) -> bool:
        return self.connected and self._ready.is_set()

    async def close(self) -> None:
        self.closed = True
        if self.close_hangs:
            await asyncio.Event().wait()
        if self.close_fails is not None:
            raise self.close_fails
        self._over.set()
        # The real one gives the loop away while the websocket closes, which is what lets `start`
        # return before its task is looked at again. Without it the task is always cancelled.
        await asyncio.sleep(0)


class FakeWorker:
    def __init__(self, *, dies: bool = False, outlives_its_cancel: bool = False) -> None:
        self.dies = dies
        self.outlives_its_cancel = outlives_its_cancel
        self.cancels = 0
        self.let_go = asyncio.Event()
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
        while self.outlives_its_cancel and not self.let_go.is_set():
            try:
                await self.let_go.wait()
            except asyncio.CancelledError:
                self.cancels += 1
        while not self.stopped:
            await asyncio.sleep(0.01)


class FakeConversations:
    """The set of threads being captured, filled the way a test says."""

    def __init__(self, *, fails: Exception | None = None, hangs: bool = False) -> None:
        self.fails = fails
        self.hangs = hangs

    async def reload(self) -> None:
        if self.hangs:
            await asyncio.Event().wait()
        if self.fails is not None:
            raise self.fails


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


class Halts:
    """Stands in for the signal a dying task sends the process.

    Injected in every test here, and not only for the ones that check it. The real one raises
    SIGTERM, which under pytest ends the run: any test whose worker or bot task dies would take
    the suite with it.
    """

    def __init__(self) -> None:
        self.asked = 0

    def __call__(self) -> None:
        self.asked += 1


async def runbuild_lifespan(bot: Any, container: Any, settings: Settings, halt: Any = None):
    app = create_app(settings=settings)
    return app, build_lifespan(bot, container, settings, halt or Halts())(app)


class TestStartingUp:
    async def test_a_database_it_cannot_reach_stops_startup(self) -> None:
        """Starting anyway would accept deliveries and fail every one of them behind the port.

        A refused connection surfaces as OSError rather than anything SQLAlchemy wraps, which
        is why the lifespan catches broadly and reports rather than matching a type. The two
        beside this one drive the same handler with a `SQLAlchemyError`, so this is the only
        place that says the catch cannot be narrowed to one.

        The address is written out rather than `localhost`, which resolves to `::1` and
        `127.0.0.1` both and so pays for two refusals. Measured here: 4.06s against 2.02s, all
        of it Windows retransmitting before it reports the refusal. Linux answers at once either
        way, so this buys nothing on CI and halves it for anybody working on Windows. The
        refusal is still the kernel's; faking it would leave the paragraph above describing a
        stub in this same function.
        """
        engine = create_async_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing")
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

    async def test_a_database_that_goes_quiet_stops_startup_rather_than_parking_it(
        self, monkeypatch: pytest.MonkeyPatch, migrated: AsyncEngine
    ) -> None:
        """asyncpg's own sixty seconds bound the handshake, not the query, so a server that takes
        the connection and then answers nothing left this waiting for as long as the kernel kept
        retrying. Uvicorn opens no socket until startup returns and reads a signal only after,
        so for all of that the process served nothing and could not be told to stop.
        """
        monkeypatch.setattr(lifespan_module, "STARTUP_CHECK_SECONDS", 0.05)

        class NeverAnswers:
            def connect(self):
                return self

            async def __aenter__(self):
                await asyncio.Event().wait()

            async def __aexit__(self, *exc: object) -> None:
                return None

        # The outer bound only stops this test hanging for ever if the deadline is not there.
        # What says the deadline fired is that it fired a hundred times sooner, so the elapsed
        # time is the assertion: `wait_for` raises the same TimeoutError and proves nothing.
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lifespan_module.require_database(NeverAnswers()), timeout=5)

        assert asyncio.get_running_loop().time() - started < 1, "it waited on the outer bound"

    async def test_without_a_token_the_worker_still_runs(self, migrated: AsyncEngine) -> None:
        worker = FakeWorker()
        app, lifespan = await runbuild_lifespan(
            FakeBot(), container_for(migrated, worker, ClosingGitHub()), settings_with()
        )

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=2)
            assert app.state.liveness.bot_connected() is True

    async def test_the_sign_in_routes_are_handed_the_verification_service(
        self, migrated: AsyncEngine
    ) -> None:
        """The OAuth routes are entered from a browser rather than called, so they read the service
        off app state, and only this puts it there. Every route test sets it by hand, so a lost
        line here left the suite green and every /oauth/* request answering 500 (#266)."""
        container = container_for(migrated, FakeWorker(), ClosingGitHub())
        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())

        async with lifespan:
            assert app.state.verification is container.verification

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

    async def test_the_health_probe_does_not_borrow_from_the_pool(
        self, migrated: AsyncEngine
    ) -> None:
        container = container_for(migrated, FakeWorker(), ClosingGitHub())
        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())

        async with lifespan:
            assert app.state.liveness.engine is not container.engine

    @pytest.mark.parametrize(
        ("conversations", "reason"),
        [
            (FakeConversations(hangs=True), "TimeoutError"),
            (FakeConversations(fails=OSError("the server hung up")), "the server hung up"),
        ],
        ids=["hangs", "raises"],
    )
    async def test_conversations_that_will_not_load_stop_startup_before_the_bot(
        self,
        migrated: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        conversations: FakeConversations,
        reason: str,
    ) -> None:
        """A lock on that one table passes the startup check and then parked startup behind it: no
        port, no log line, and nothing able to stop the process (#272).

        The check itself is stood in for, because the deadline shortened here is its deadline too.
        """

        async def answers(engine: AsyncEngine) -> None:
            return None

        monkeypatch.setattr(lifespan_module, "require_database", answers)
        monkeypatch.setattr(lifespan_module, "STARTUP_CHECK_SECONDS", 0.05)
        worker, bot = FakeWorker(), FakeBot()
        container = container_for(migrated, worker, ClosingGitHub())
        container.conversations = conversations
        _, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        # Watched rather than bounded, since an outer deadline would raise the very same error.
        with caplog.at_level("ERROR"):
            entering = asyncio.ensure_future(lifespan.__aenter__())
            await asyncio.wait({entering}, timeout=10)

        assert entering.done(), "startup was parked behind the query"
        with pytest.raises(OSError):
            entering.result()
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "shannon.runtime.lifespan"
        ] == [f"could not load which conversations are being logged: {reason}"]
        assert bot.started is False
        assert not worker.ran.is_set()


class TestWhileRunning:
    async def test_a_gateway_still_trying_to_connect_is_not_reported_as_connected(
        self, migrated: AsyncEngine
    ) -> None:
        """discord.py reconnects for ever rather than giving up, so an outage or blocked egress
        leaves `start()` running and the connection never made. Answering on the task being
        alive called that healthy, which is the one state the endpoint exists to report.
        """
        bot = FakeBot(connects=True, reaches_the_gateway=False)
        worker = FakeWorker()
        app, lifespan = await runbuild_lifespan(
            bot, container_for(migrated, worker, ClosingGitHub()), settings_with("a-token")
        )

        async with lifespan:
            await until(lambda: bot.started)
            assert app.state.liveness.bot_connected() is False, "a bot that never arrived"

    async def test_a_gateway_that_goes_after_it_came_up_is_not_reported_as_connected(
        self, migrated: AsyncEngine
    ) -> None:
        """discord.py's own `is_ready` latches, so asked instead this would go on answering yes."""
        bot, worker = FakeBot(), FakeWorker()
        app, lifespan = await runbuild_lifespan(
            bot, container_for(migrated, worker, ClosingGitHub()), settings_with("a-token")
        )

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)
            assert app.state.liveness.bot_connected() is True

            bot.connected = False

            assert app.state.liveness.bot_connected() is False

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
            await until(lambda: not app.state.liveness.worker_running())
            assert app.state.liveness.bot_connected() is False


class TestShuttingDown:
    async def test_a_clean_shutdown_says_nothing_alarming(
        self, migrated: AsyncEngine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole point of the warning is that it is rare enough to be worth reading.

        Both tasks end on a normal shutdown exactly the way they end on a failure: the worker's
        loop returns when told to stop, and the Discord client's start returns once it is closed.
        Reported either way, the line meaning the process is now useless appeared on every
        deployment restart, which is the fastest way to teach everyone to ignore it.

        Nothing at all from the runtime, so a loop cut off or a step that failed is seen here too.
        The default grace, which nothing waits out: the poller, flusher and sender are the real
        ones, and the short grace the other tests use could cut one off on a loaded machine.
        """
        worker = FakeWorker()
        bot = FakeBot()
        settings = Settings(github_webhook_secret="x", discord_token="a-token")
        _, lifespan = await runbuild_lifespan(
            bot, container_for(migrated, worker, ClosingGitHub()), settings
        )

        with caplog.at_level("WARNING", logger="shannon.runtime"):
            async with lifespan:
                await asyncio.wait_for(worker.ran.wait(), timeout=2)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("shannon.runtime") and record.levelno >= logging.WARNING
        ] == []

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
        app, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        async with lifespan:
            await until(lambda: not app.state.liveness.worker_running())

        assert bot.closed is True, "the Discord client was left open"
        assert github.closed is True, "the engine and HTTP client were left open"

    async def test_a_worker_that_outlives_its_cancel_does_not_stop_the_rest_closing(
        self,
        migrated: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Waited for with no limit, it held the Discord client, the HTTP clients and the pool open
        for as long as it took, and nothing said what shutdown was waiting for (#272)."""
        monkeypatch.setattr(lifespan_module, "CANCEL_GRACE_SECONDS", 0.05)
        worker, bot, github = FakeWorker(outlives_its_cancel=True), FakeBot(), ClosingGitHub()
        container = container_for(migrated, worker, github)
        _, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        try:
            async with asyncio.timeout(20):
                async with lifespan:
                    await asyncio.wait_for(worker.ran.wait(), timeout=5)
                assert worker.cancels == 1
        finally:
            worker.let_go.set()

        assert bot.closed is True, "the Discord client was left open"
        assert github.closed is True, "the engine and HTTP client were left open"
        messages = [record.getMessage() for record in caplog.records]
        assert "the delivery worker did not stop when asked, cancelling it" in messages
        assert (
            "the delivery worker is still running 0.05s after being cancelled, leaving it"
            in messages
        )

    @pytest.mark.parametrize(
        ("how", "reason"),
        [
            ({"close_hangs": True}, "still not done after 0.5s"),
            ({"close_fails": asyncio.CancelledError}, "it was cancelled"),
            ({"close_fails": TimeoutError}, "TimeoutError"),
        ],
        ids=["hangs", "is cancelled", "times out"],
    )
    async def test_a_discord_client_that_will_not_close_does_not_stop_the_rest_closing(
        self,
        migrated: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        how: dict[str, Any],
        reason: str,
    ) -> None:
        """One that never returned held the process up, and one that ended in `CancelledError` had
        every step after it skipped (#272). The bot's own task, left running by all three, is
        cancelled without a word: the close has said what there is to say."""
        monkeypatch.setattr(lifespan_module, "CLOSE_SECONDS", 0.5)
        worker, bot, github = FakeWorker(), FakeBot(**how), ClosingGitHub()
        container = container_for(migrated, worker, github)
        app, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))

        async with asyncio.timeout(20):
            async with lifespan:
                await asyncio.wait_for(worker.ran.wait(), timeout=5)

        assert github.closed is True, "the engine and HTTP client were left open"
        assert app.state.liveness.bot_connected() is False, "the bot was left running"
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("shannon.runtime") and "Discord" in record.getMessage()
        ] == [f"could not close the Discord client while shutting down: {reason}"]

    async def test_a_container_that_will_not_close_does_not_hold_the_process(
        self,
        migrated: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A pooled connection to a database gone quiet closes when the kernel gives up on it."""

        async def never(container: Container) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(lifespan_module, "CLOSE_SECONDS", 0.05)
        monkeypatch.setattr(Container, "aclose", never)
        worker = FakeWorker()
        container = container_for(migrated, worker, ClosingGitHub())
        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())

        async with asyncio.timeout(20):
            async with lifespan:
                await asyncio.wait_for(worker.ran.wait(), timeout=5)

        assert "could not close the container while shutting down: still not done after 0.05s" in [
            record.getMessage() for record in caplog.records
        ]


class FakePoller:
    """A project poller with the shape the lifespan uses, and a switch for whether it runs."""

    def __init__(self, *, enabled: bool, dies: bool = False) -> None:
        self.enabled = enabled
        self.dies = dies
        self.stopped = False
        self.ran = asyncio.Event()

    def stop(self) -> None:
        self.stopped = True

    async def run_forever(self) -> None:
        self.ran.set()
        if self.dies:
            raise RuntimeError("the poller fell over")
        while not self.stopped:
            await asyncio.sleep(0.01)


class TestATaskTheProcessCannotDoWithout:
    """A dead worker used to leave a live process, and nothing anywhere ended it.

    The endpoint went on answering 200 to deliveries nothing would ever work, `/health` went on
    answering 503, and the container sat there: the restart policy acts on an exit and never on
    the health state, so an unhealthy container that has not exited is not restarted. Half of
    what kills these tasks is fixed by a restart, a rotated token first among them.
    """

    async def test_a_worker_that_falls_over_asks_the_process_to_stop(
        self, migrated: AsyncEngine
    ) -> None:
        halt = Halts()
        worker = FakeWorker(dies=True)
        container = container_for(migrated, worker, ClosingGitHub())
        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with(), halt)

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)
            await until(lambda: halt.asked > 0)

        assert halt.asked == 1

    async def test_a_bot_that_gives_up_asks_the_process_to_stop(
        self, migrated: AsyncEngine
    ) -> None:
        """discord.py reconnects on its own, so `start` returning means it will not come back.

        The worker only waits for the gateway once, at the start, so a connection lost after
        that leaves it leasing deliveries that every one of them fails.

        Twice, because the worker was still waiting for that connection and ends with it.
        """
        halt = Halts()
        worker = FakeWorker()
        container = container_for(migrated, worker, ClosingGitHub())
        _, lifespan = await runbuild_lifespan(
            FakeBot(connects=False), container, settings_with("a-token"), halt
        )

        async with lifespan:
            await until(lambda: halt.asked == 2)

        assert halt.asked == 2

    async def test_an_ordinary_shutdown_asks_for_nothing(self, migrated: AsyncEngine) -> None:
        """Every task ends at shutdown, and reading that as a failure would loop the container."""
        halt = Halts()
        worker = FakeWorker()
        container = container_for(migrated, worker, ClosingGitHub())
        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with("a-token"), halt)

        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)

        assert halt.asked == 0

    async def test_a_poller_that_dies_leaves_the_process_alone(self, migrated: AsyncEngine) -> None:
        """Webhooks still mirror everything without it, so stopping over one is worse."""
        halt = Halts()
        worker, poller = FakeWorker(), FakePoller(enabled=True, dies=True)
        container = container_for(migrated, worker, ClosingGitHub())
        container.poller = poller

        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with(), halt)
        async with lifespan:
            await until(lambda: not app.state.liveness.poller_running())

        assert poller.ran.is_set()
        assert halt.asked == 0


class TestTheProjectPoller:
    """Started unless SHANNON_POLL_BOARDS is off, and stopped whether it is or not.

    On is the default. Off, starting a task that returned at once would have the done callback
    report the poller as having stopped on every boot.
    """

    async def test_with_polling_on_boards_are_polled(self, migrated: AsyncEngine) -> None:
        worker, poller = FakeWorker(), FakePoller(enabled=True)
        container = container_for(migrated, worker, ClosingGitHub())
        container.poller = poller

        _, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())
        async with lifespan:
            await asyncio.wait_for(poller.ran.wait(), timeout=5)

        assert poller.stopped is True

    async def test_with_polling_off_there_is_no_task_at_all(self, migrated: AsyncEngine) -> None:
        worker, poller = FakeWorker(), FakePoller(enabled=False)
        container = container_for(migrated, worker, ClosingGitHub())
        container.poller = poller

        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())
        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)
            assert app.state.liveness.tasks.poller_task is None

        assert not poller.ran.is_set(), "boards were polled with polling switched off"
        # Still told to stop: the flag costs nothing and means shutdown has one path, not two.
        assert poller.stopped is True


class FakeSender:
    """A reminder sender with the shape the lifespan uses. Issue #229."""

    def __init__(self, *, dies: bool = False) -> None:
        self.dies = dies
        self.stopped = False
        self.ran = asyncio.Event()

    def stop(self) -> None:
        self.stopped = True

    async def run_forever(self, wait_for_ready: ReadyCheck) -> None:
        await wait_for_ready()
        self.ran.set()
        if self.dies:
            raise RuntimeError("the reminder sender fell over")
        while not self.stopped:
            await asyncio.sleep(0.01)


def container_sending(engine: AsyncEngine, sender: FakeSender) -> tuple[Container, FakeWorker]:
    worker = FakeWorker()
    container = container_for(engine, worker, ClosingGitHub())
    container.reminders = sender  # type: ignore[assignment]
    return container, worker


class TestTheReminderSender:
    """Started only where there is a gateway, only once it is up, and halting nothing. Issue #229.

    A reminder goes to Discord and nowhere else, so a sender started without a gateway would wait
    for a connection that never comes and be reported as running all the while.
    """

    async def test_with_a_token_it_runs_once_the_gateway_is_up(self, migrated: AsyncEngine) -> None:
        sender = FakeSender()
        container, _ = container_sending(migrated, sender)

        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with("a-token"))
        async with lifespan:
            await asyncio.wait_for(sender.ran.wait(), timeout=5)
            assert app.state.liveness.reminders_running() is True

        assert sender.stopped is True

    async def test_it_waits_for_a_gateway_still_connecting(self, migrated: AsyncEngine) -> None:
        sender = FakeSender()
        container, _ = container_sending(migrated, sender)

        bot = FakeBot(reaches_the_gateway=False)
        _, lifespan = await runbuild_lifespan(bot, container, settings_with("a-token"))
        async with lifespan:
            # The worker and the sender both, so the sender has had its chance to run.
            await until(lambda: bot.waiting == 2)
            assert not sender.ran.is_set(), "reminders went out before Discord was there"

        assert sender.stopped is True

    async def test_without_a_token_there_is_no_task_at_all(self, migrated: AsyncEngine) -> None:
        sender = FakeSender()
        container, worker = container_sending(migrated, sender)

        app, lifespan = await runbuild_lifespan(FakeBot(), container, settings_with())
        async with lifespan:
            await asyncio.wait_for(worker.ran.wait(), timeout=5)
            # No task at all, rather than one waiting for a gateway that never comes: the second
            # would read as running here too, which is the very thing this guards against.
            assert app.state.liveness.tasks.reminders_task is None
            assert app.state.liveness.reminders_running() is True

        assert not sender.ran.is_set(), "a sender ran with no gateway to send to"
        # Still told to stop, like a poller with no board: shutdown has one path, not two.
        assert sender.stopped is True

    async def test_one_that_dies_leaves_the_process_alone(self, migrated: AsyncEngine) -> None:
        """Everything else still works, and what it had not sent waits for the next process."""
        halt = Halts()
        sender = FakeSender(dies=True)
        container, _ = container_sending(migrated, sender)

        app, lifespan = await runbuild_lifespan(
            FakeBot(), container, settings_with("a-token"), halt
        )
        async with lifespan:
            await until(lambda: not app.state.liveness.reminders_running())

        assert sender.ran.is_set()
        assert halt.asked == 0
