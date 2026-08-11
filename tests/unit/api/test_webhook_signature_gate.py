from __future__ import annotations

import pytest

from shannon.github.webhooks.signature import sign
from tests.support.webhooks import SECRET, RecordingHandler, build_client, post


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


async def test_missing_signature_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"}, signature=None)

    assert response.status_code == 401
    assert "missing" in response.json()["detail"]
    assert handler.calls == []


async def test_malformed_signature_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"}, signature="deadbeef")

    assert response.status_code == 401
    assert "malformed" in response.json()["detail"]
    assert handler.calls == []


async def test_signature_from_the_wrong_secret_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(
            client, "pull_request", {"action": "opened"}, secret="not-the-real-secret"
        )

    assert response.status_code == 401
    assert handler.calls == []


async def test_signature_over_a_different_body_is_rejected(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(
            client,
            "pull_request",
            body=b'{"action": "opened", "number": 7}',
            signature=sign(b'{"action": "opened"}', SECRET),
        )

    assert response.status_code == 401
    assert handler.calls == []


async def test_valid_signature_is_accepted(handler: RecordingHandler) -> None:
    async with build_client(handler) as client:
        response = await post(client, "pull_request", {"action": "opened"})

    assert response.status_code == 200
    assert handler.calls != []


async def test_unconfigured_secret_rejects_every_delivery(handler: RecordingHandler) -> None:
    async with build_client(handler, secret="") as client:
        response = await post(client, "pull_request", {"action": "opened"}, secret="")

    assert response.status_code == 500
    assert response.json()["detail"] == "Webhook secret is not configured"
    assert handler.calls == []


async def test_signature_is_checked_before_the_body_is_parsed(handler: RecordingHandler) -> None:
    """A forged request is turned away without its payload being trusted at all."""
    async with build_client(handler) as client:
        response = await post(client, "pull_request", body=b"{not json", signature=None)

    assert response.status_code == 401
