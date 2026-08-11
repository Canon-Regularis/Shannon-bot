from __future__ import annotations

import logging

import pytest

from shannon.github.webhooks.events import WebhookOutcome
from shannon.services.idempotency import DeliveryStatus
from tests.fakes.guards import InMemoryDeliveryGuard
from tests.support.webhooks import RecordingHandler, build_client, post

PR_OPENED = {"action": "opened", "number": 7}


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


@pytest.fixture
def guard() -> InMemoryDeliveryGuard:
    return InMemoryDeliveryGuard()


async def test_repeated_delivery_reaches_the_handler_once(
    handler: RecordingHandler, guard: InMemoryDeliveryGuard
) -> None:
    async with build_client(handler, delivery_guard=guard) as client:
        first = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        second = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate"
    assert second.status_code == 200
    assert len(handler.calls) == 1


async def test_different_deliveries_both_run(
    handler: RecordingHandler, guard: InMemoryDeliveryGuard
) -> None:
    async with build_client(handler, delivery_guard=guard) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        await post(client, "pull_request", PR_OPENED, delivery="delivery-b")

    assert len(handler.calls) == 2


async def test_processed_delivery_is_recorded_as_processed(
    handler: RecordingHandler, guard: InMemoryDeliveryGuard
) -> None:
    async with build_client(handler, delivery_guard=guard) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert guard.completed == {"delivery-a": DeliveryStatus.PROCESSED}


async def test_ignored_delivery_is_recorded_as_ignored(guard: InMemoryDeliveryGuard) -> None:
    handler = RecordingHandler(outcome=WebhookOutcome.IGNORED)
    async with build_client(handler, delivery_guard=guard) as client:
        await post(client, "pull_request", {"action": "synchronize"}, delivery="delivery-a")

    assert guard.completed == {"delivery-a": DeliveryStatus.IGNORED}


async def test_unsigned_duplicate_never_reaches_the_guard(guard: InMemoryDeliveryGuard) -> None:
    async with build_client(RecordingHandler(), delivery_guard=guard) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a", signature=None)

    assert guard.claimed == set()


async def test_failed_handler_releases_the_claim(guard: InMemoryDeliveryGuard) -> None:
    class ExplodingHandler(RecordingHandler):
        async def __call__(self, action, payload):  # type: ignore[no-untyped-def]
            await super().__call__(action, payload)
            raise RuntimeError("discord is down")

    handler = ExplodingHandler()
    async with build_client(handler, delivery_guard=guard) as client:
        with pytest.raises(RuntimeError):
            await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert guard.released == ["delivery-a"]
    assert guard.claimed == set()


async def test_retry_after_failure_is_processed(guard: InMemoryDeliveryGuard) -> None:
    """GitHub retries a failed delivery with the same ID; that retry has to be real work."""
    attempts: list[str] = []

    class FlakyHandler(RecordingHandler):
        async def __call__(self, action, payload):  # type: ignore[no-untyped-def]
            attempts.append(action)
            if len(attempts) == 1:
                raise RuntimeError("discord is down")
            return await super().__call__(action, payload)

    handler = FlakyHandler()
    async with build_client(handler, delivery_guard=guard) as client:
        with pytest.raises(RuntimeError):
            await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        retry = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert retry.json()["status"] == "processed"
    assert len(handler.calls) == 1


async def test_duplicate_is_logged(
    handler: RecordingHandler, guard: InMemoryDeliveryGuard, caplog: pytest.LogCaptureFixture
) -> None:
    async with build_client(handler, delivery_guard=guard) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        with caplog.at_level(logging.INFO, logger="shannon.api.routes.webhooks"):
            await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert "outcome=duplicate" in caplog.text
