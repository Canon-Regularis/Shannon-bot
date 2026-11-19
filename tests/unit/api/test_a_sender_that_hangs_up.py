"""A webhook sender that hangs up before the body has all arrived (#266).

Starlette raises `ClientDisconnect` while the body is read. Nothing caught it, so uvicorn logged
an ERROR with a full traceback, and anybody could write one by starting a POST to the open
endpoint and dropping it. It is one WARNING now, and nothing is recorded.

Driven over raw ASGI rather than through httpx, because a client that leaves mid-body is the
whole of the case and httpx's transport has no way to be one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.github.webhooks.router import EventRouter
from tests.fakes.handlers import RecordingHandler

pytestmark = pytest.mark.unit

SCOPE: dict[str, Any] = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "POST",
    "scheme": "http",
    "path": "/webhooks/github",
    "raw_path": b"/webhooks/github",
    "query_string": b"",
    "root_path": "",
    "headers": [
        (b"content-type", b"application/json"),
        (b"x-github-event", b"pull_request"),
        (b"x-github-delivery", b"left-early"),
    ],
    "client": ("127.0.0.1", 5000),
    "server": ("test", 80),
}


async def leave_after(first: bytes, app: Any) -> tuple[int, dict[str, Any]]:
    """Send `first`, say more is coming, then hang up. The status and body the app answered."""
    arriving: list[dict[str, Any]] = [
        {"type": "http.request", "body": first, "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return arriving.pop(0) if arriving else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(dict(SCOPE), receive, send)

    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


async def test_it_is_one_warning_and_nothing_is_recorded(caplog: pytest.LogCaptureFixture) -> None:
    handler = RecordingHandler()
    event_router = EventRouter()
    event_router.register("pull_request", handler)
    app = create_app(
        settings=Settings(github_webhook_secret=SecretStr("x")), event_router=event_router
    )

    with caplog.at_level(logging.WARNING):
        status, body = await leave_after(b'{"action": "op', app)

    assert status == 400
    assert body == {"detail": "The body did not arrive whole"}
    assert (
        "shannon.api.routes.webhooks",
        logging.WARNING,
        "abandoning a delivery whose sender left after 14 bytes",
    ) in caplog.record_tuples
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert handler.calls == []
