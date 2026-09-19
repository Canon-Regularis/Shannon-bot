from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from shannon.api.app import create_app
from shannon.config import Settings
from tests.fakes.liveness import FakeLiveness


def client_with(liveness: object | None, **overrides: str) -> AsyncClient:
    """Overrides passed through rather than named with defaults of their own.

    `build` has a default in `Settings` and giving this one to match would mean every test that
    reads it back is asserting the value written here. Which is what happened: changing the real
    default to an empty string went green.
    """
    app = create_app(settings=Settings(github_webhook_secret="x", **overrides))
    app.state.liveness = liveness
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_a_working_process_reports_healthy() -> None:
    async with client_with(FakeLiveness()) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "healthy": True,
        "database": True,
        "worker": True,
        "bot": True,
        "poller": True,
        "flusher": True,
        "version": "unknown",
    }


async def test_a_dead_board_poller_is_reported_without_failing_the_check() -> None:
    """The one thing here that is said without being counted.

    This process is still doing its job without the board: webhooks arrive, threads are written,
    and only board movement stops. Failing the check would have an orchestrator restart a working
    process and throw away whatever the worker had in hand.

    Saying nothing is the other mistake and the one this exists to stop. The poller is the only
    task with nothing wired to halt the process when it dies, so it goes with one line in the log
    and everything afterwards answers that all is well.
    """
    async with client_with(FakeLiveness(poller=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["healthy"] is True, "a dead board restarted a working process"
    assert response.json()["poller"] is False, "a dead board was not reported at all"


async def test_a_dead_transcript_flusher_is_reported_without_failing_the_check() -> None:
    """The second thing said without being counted, and its own line rather than folded into the
    board's: they fail for unrelated reasons and mean unrelated things. Issue #103.

    Not counted because the rest of the process is unharmed. Webhooks arrive, threads are written,
    and capture itself carries on into the table, so what is held there is published by the next
    process with a working flusher. Restarting over this would throw away a working worker's batch
    to fix something that is already waiting patiently.

    Said at all because nothing halts the process when this task dies, so without a line here it
    goes with one log entry while what people said piles up in a table for ever.
    """
    async with client_with(FakeLiveness(flusher=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["healthy"] is True, "a dead flusher restarted a working process"
    assert response.json()["flusher"] is False, "a dead flusher was not reported at all"


async def test_an_unhealthy_process_does_not_also_complain_about_the_flusher() -> None:
    """One line about what is actually wrong. A process whose database has gone reports that; the
    flusher being down as well is a consequence, not a second thing to go and look at."""
    async with client_with(FakeLiveness(database=False, flusher=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["flusher"] is False


async def test_no_board_configured_is_not_something_stopped() -> None:
    """Which is the default: no board is set up unless somebody sets one up."""
    async with client_with(FakeLiveness(poller=True)) as client:
        response = await client.get("/health")

    assert response.json()["poller"] is True


async def test_a_dead_worker_makes_the_process_unhealthy() -> None:
    """The whole point: the port is open and deliveries are accepted, but nothing acts on them."""
    async with client_with(FakeLiveness(worker=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["worker"] is False


async def test_an_unreachable_database_makes_the_process_unhealthy() -> None:
    async with client_with(FakeLiveness(database=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["database"] is False


async def test_with_nothing_wired_in_it_only_claims_to_be_listening() -> None:
    """How the route-level tests run. Claiming more than it knows would be worse than useless."""
    async with client_with(None) as client:
        response = await client.get("/health")

    assert response.status_code == 200


class TestWhichBuildIsAnswering:
    """The question `/health` could not answer, and the reason this field exists.

    A change that was merged and never deployed and a change that does not work look the same
    from outside: the thread renders without it either way. Telling them apart meant getting onto
    the box, and key auth was never set up on that one.
    """

    async def test_the_stamped_commit_is_reported(self) -> None:
        async with client_with(FakeLiveness(), build="e261061") as client:
            response = await client.get("/health")

        assert response.json()["version"] == "e261061"

    async def test_an_unstamped_image_says_so_rather_than_nothing(self) -> None:
        """An empty string reads as a bug in the reporting. `unknown` reads as what it is, which
        is an image somebody built on their laptop."""
        async with client_with(FakeLiveness()) as client:
            response = await client.get("/health")

        assert response.json()["version"] == "unknown"

    async def test_a_process_with_nothing_wired_in_still_says_which_build_it_is(self) -> None:
        """The route's other return, which claims the least it can about everything else. Which
        commit is running is not something it needs a worker to be able to say."""
        async with client_with(None, build="e261061") as client:
            response = await client.get("/health")

        assert response.json()["version"] == "e261061"

    async def test_an_unhealthy_process_still_says_which_build_it_is(self) -> None:
        """The answer is wanted most when something is wrong, and 503 takes the other return."""
        async with client_with(FakeLiveness(worker=False), build="e261061") as client:
            response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["version"] == "e261061"


async def test_a_dead_gateway_makes_the_process_unhealthy() -> None:
    """The worker only waits for the bot once, so a gateway that dies later leaves it leasing.

    Reporting only the worker would call that healthy while every Discord call fails.
    """
    async with client_with(FakeLiveness(bot=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["bot"] is False
    assert response.json()["worker"] is True
