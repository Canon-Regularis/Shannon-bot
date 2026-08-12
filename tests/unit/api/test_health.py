from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from shannon.api.app import create_app
from shannon.config import Settings


class FakeLiveness:
    def __init__(self, *, database: bool = True, worker: bool = True) -> None:
        self.database = database
        self.worker = worker

    async def database_reachable(self) -> bool:
        return self.database

    def worker_running(self) -> bool:
        return self.worker


def client_with(liveness: object | None) -> AsyncClient:
    app = create_app(settings=Settings(github_webhook_secret="x"))
    app.state.liveness = liveness
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_a_working_process_reports_healthy() -> None:
    async with client_with(FakeLiveness()) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"healthy": True, "database": True, "worker": True}


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
