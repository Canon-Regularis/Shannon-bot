from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from shannon.api.app import create_app
from shannon.config import Settings
from tests.fakes.liveness import FakeLiveness


def client_with(liveness: object | None) -> AsyncClient:
    app = create_app(settings=Settings(github_webhook_secret="x"))
    app.state.liveness = liveness
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_a_working_process_reports_healthy() -> None:
    async with client_with(FakeLiveness()) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"healthy": True, "database": True, "worker": True, "bot": True}


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


async def test_a_dead_gateway_makes_the_process_unhealthy() -> None:
    """The worker only waits for the bot once, so a gateway that dies later leaves it leasing.

    Reporting only the worker would call that healthy while every Discord call fails.
    """
    async with client_with(FakeLiveness(bot=False)) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["bot"] is False
    assert response.json()["worker"] is True
