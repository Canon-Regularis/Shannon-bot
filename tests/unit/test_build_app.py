"""The real assembly, run once.

Everything else stands something in: the container tests fake Discord and GitHub, the lifespan
tests fake the bot and the worker. Nothing exercised `build_app` itself, which is the only place
the real ShannonBot, the real thread gateway and the real container are put together. Each of
those steps can break without another test noticing, and running it once is what catches that.

No database or network is touched. Building an engine connects to nothing, and a Discord client
does not reach the gateway until it is started.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shannon.config import Settings
from shannon.main import build_app, configure_logging, run

SETTINGS = Settings(github_webhook_secret="x")


@pytest.fixture
def app() -> FastAPI:
    return build_app(SETTINGS)


def test_the_whole_thing_assembles(app: FastAPI) -> None:
    """The bot, the gateway that needs it, the container that needs the gateway, and the
    lifespan that needs all three. Anything raising in that chain fails here."""
    assert isinstance(app, FastAPI)


async def test_it_serves_the_two_routes(app: FastAPI) -> None:
    """Asked rather than introspected: FastAPI wraps an included router in something whose
    `path` is not the route's, so reading the table tells you less than a request does."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/health")
        webhook = await client.post("/webhooks/github")

    assert health.status_code == 200
    # No headers, so the route rejects it. What matters is that something answered.
    assert webhook.status_code == 400


def test_the_endpoint_is_given_a_queue_to_write_to(app: FastAPI) -> None:
    """Without one the route handles deliveries inline, which is what the queue exists to stop.
    That fallback is for route-level tests and must never be what production gets."""
    assert app.state.delivery_queue is not None


def test_the_router_is_wired_to_a_handler_for_every_event(app: FastAPI) -> None:
    for event in ("pull_request", "issues", "issue_comment", "pull_request_review"):
        assert app.state.event_router.handles(event), f"{event} would be dropped on arrival"


def test_the_settings_it_was_given_are_the_ones_it_uses(app: FastAPI) -> None:
    assert app.state.settings is SETTINGS


class TestStartingTheProcess:
    """`run` is the entry point the console script calls, and nothing had ever called it."""

    def test_the_port_it_serves_on_is_the_configured_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SHANNON_API_HOST and SHANNON_API_PORT are documented settings that nothing read.

        Bound to the wrong port the process comes up, answers its own health check, and takes
        no delivery, because GitHub is posting somewhere nothing is listening.
        """
        served: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            served.update(kwargs)
            served["app"] = app

        monkeypatch.setattr("shannon.main.uvicorn.run", fake_run)
        monkeypatch.setattr("shannon.main.get_settings", lambda: SETTINGS)
        monkeypatch.setattr("shannon.main.configure_logging", lambda settings: None)

        run()

        assert served["host"] == SETTINGS.api_host
        assert served["port"] == SETTINGS.api_port
        assert isinstance(served["app"], FastAPI)

    def test_logging_is_set_up_from_the_configured_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checked through basicConfig rather than by reading the root logger back, which other
        tests in this process share and would be left holding whatever this set."""
        configured: dict[str, object] = {}
        monkeypatch.setattr("shannon.main.logging.basicConfig", lambda **kw: configured.update(kw))

        configure_logging(Settings(log_level="warning"))

        assert configured["level"] == "WARNING"
