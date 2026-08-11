from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shannon.github.webhooks.events import EventRouter
from tests.support.webhooks import RecordingHandler, build_client, post


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


@pytest_asyncio.fixture
async def client(handler: RecordingHandler) -> AsyncIterator[AsyncClient]:
    async with build_client(handler) as http_client:
        yield http_client


async def test_supported_event_reaches_its_handler(
    client: AsyncClient, handler: RecordingHandler
) -> None:
    payload = {"action": "opened", "number": 7}

    response = await post(client, "pull_request", payload)

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "event": "pull_request", "action": "opened"}
    assert handler.calls == [("opened", payload)]


async def test_unsupported_event_is_ignored_without_error(
    client: AsyncClient, handler: RecordingHandler
) -> None:
    response = await post(client, "issues", {"action": "opened"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert handler.calls == []


async def test_unsupported_action_on_supported_event_is_ignored(
    client: AsyncClient, handler: RecordingHandler
) -> None:
    response = await post(client, "pull_request", {"action": "synchronize"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert handler.calls == []


async def test_ping_is_acknowledged(client: AsyncClient, handler: RecordingHandler) -> None:
    response = await post(client, "ping", {"zen": "Keep it logically awesome."})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert handler.calls == []


async def test_missing_event_header_is_rejected(client: AsyncClient) -> None:
    response = await post(client, "", {"action": "opened"})

    assert response.status_code == 400
    assert "X-GitHub-Event" in response.json()["detail"]


async def test_missing_delivery_header_is_rejected(client: AsyncClient) -> None:
    response = await post(client, "pull_request", {"action": "opened"}, delivery="")

    assert response.status_code == 400
    assert "X-GitHub-Delivery" in response.json()["detail"]


async def test_malformed_json_is_rejected(client: AsyncClient) -> None:
    response = await post(client, "pull_request", body=b"{not json")

    assert response.status_code == 400
    assert response.json()["detail"] == "Body is not valid JSON"


async def test_non_object_body_is_rejected(client: AsyncClient) -> None:
    response = await post(client, "pull_request", body=b"[1, 2, 3]")

    assert response.status_code == 400
    assert response.json()["detail"] == "Body must be a JSON object"


async def test_non_string_action_is_rejected(client: AsyncClient) -> None:
    response = await post(client, "pull_request", {"action": 7})

    assert response.status_code == 400
    assert response.json()["detail"] == "Payload action must be a string"


async def test_payload_without_action_is_ignored(
    client: AsyncClient, handler: RecordingHandler
) -> None:
    response = await post(client, "pull_request", {"number": 7})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert handler.calls == []


async def test_supported_event_without_registered_handler_is_ignored() -> None:
    async with build_client(None) as client:
        response = await post(client, "pull_request", {"action": "opened"})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_registering_an_unsupported_event_fails() -> None:
    with pytest.raises(ValueError, match="not a supported webhook event"):
        EventRouter().register("issues", RecordingHandler())
