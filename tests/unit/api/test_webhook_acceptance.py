from __future__ import annotations

import logging

import pytest

from tests.fakes.queues import InMemoryDeliveryQueue
from tests.support.webhooks import RecordingHandler, build_client, post

PR_OPENED = {"action": "opened", "number": 7}


@pytest.fixture
def queue() -> InMemoryDeliveryQueue:
    return InMemoryDeliveryQueue()


async def test_a_delivery_is_accepted_without_the_handler_running(
    handler: RecordingHandler, queue: InMemoryDeliveryQueue
) -> None:
    """The whole point of the queue: nothing slow happens inside the ten seconds GitHub allows."""
    async with build_client(handler, queue=queue) as client:
        response = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert response.json()["status"] == "accepted"
    assert queue.ids == ["delivery-a"]
    assert handler.calls == []


async def test_a_repeated_delivery_is_queued_once(
    handler: RecordingHandler, queue: InMemoryDeliveryQueue
) -> None:
    async with build_client(handler, queue=queue) as client:
        first = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        second = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert second.status_code == 200
    assert queue.ids == ["delivery-a"]


async def test_different_deliveries_are_both_queued(
    handler: RecordingHandler, queue: InMemoryDeliveryQueue
) -> None:
    async with build_client(handler, queue=queue) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        await post(client, "pull_request", PR_OPENED, delivery="delivery-b")

    assert queue.ids == ["delivery-a", "delivery-b"]


async def test_the_queued_payload_is_the_body_that_arrived(queue: InMemoryDeliveryQueue) -> None:
    """The worker replays this later, so it has to be the whole thing."""
    async with build_client(RecordingHandler(), queue=queue) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    _, event_type, payload = queue.enqueued[0]
    assert event_type == "pull_request"
    assert payload == PR_OPENED


async def test_an_unsupported_action_is_never_queued(queue: InMemoryDeliveryQueue) -> None:
    """Nothing to protect against a repeat of something that would be dropped anyway."""
    async with build_client(RecordingHandler(), queue=queue) as client:
        response = await post(
            client, "pull_request", {"action": "synchronize"}, delivery="delivery-a"
        )

    assert response.json()["status"] == "ignored"
    assert queue.enqueued == []


async def test_an_unsupported_event_is_never_queued(queue: InMemoryDeliveryQueue) -> None:
    async with build_client(RecordingHandler(), queue=queue) as client:
        await post(client, "project_card", {"action": "created"}, delivery="delivery-a")

    assert queue.enqueued == []


async def test_an_event_with_no_handler_is_never_queued(queue: InMemoryDeliveryQueue) -> None:
    """Queueing work nobody can do would leave rows retrying until they gave up."""
    async with build_client(None, queue=queue) as client:
        response = await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert response.json()["status"] == "ignored"
    assert queue.enqueued == []


async def test_a_ping_is_never_queued(queue: InMemoryDeliveryQueue) -> None:
    async with build_client(RecordingHandler(), queue=queue) as client:
        await post(client, "ping", {"zen": "Keep it logically awesome."}, delivery="delivery-a")

    assert queue.enqueued == []


async def test_an_unsigned_delivery_never_reaches_the_queue(queue: InMemoryDeliveryQueue) -> None:
    async with build_client(RecordingHandler(), queue=queue) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a", signature=None)

    assert queue.enqueued == []


async def test_a_duplicate_is_logged(
    handler: RecordingHandler, queue: InMemoryDeliveryQueue, caplog: pytest.LogCaptureFixture
) -> None:
    async with build_client(handler, queue=queue) as client:
        await post(client, "pull_request", PR_OPENED, delivery="delivery-a")
        with caplog.at_level(logging.INFO, logger="shannon.api.routes.webhooks"):
            await post(client, "pull_request", PR_OPENED, delivery="delivery-a")

    assert "outcome=duplicate" in caplog.text
