from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import WebhookEvent
from shannon.services.idempotency import DeliveryStatus, WebhookIdempotencyGuard

pytestmark = pytest.mark.integration

BODY = b'{"action": "opened", "number": 7}'


@pytest.fixture
def guard(db_sessionmaker: async_sessionmaker) -> WebhookIdempotencyGuard:
    return WebhookIdempotencyGuard(db_sessionmaker)


async def count_events(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(WebhookEvent)) or 0


async def test_first_claim_wins_and_second_loses(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    assert await guard.claim("delivery-a", "pull_request", BODY) is True
    assert await guard.claim("delivery-a", "pull_request", BODY) is False

    assert await count_events(db_session) == 1


async def test_concurrent_claims_produce_exactly_one_winner(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    results = await asyncio.gather(
        *(guard.claim("delivery-a", "pull_request", BODY) for _ in range(8))
    )

    assert results.count(True) == 1
    assert await count_events(db_session) == 1


async def test_claim_records_the_payload_hash(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    await guard.claim("delivery-a", "pull_request", BODY)

    event = await db_session.scalar(
        select(WebhookEvent).where(WebhookEvent.github_delivery_id == "delivery-a")
    )
    assert event is not None
    assert event.payload_hash == hashlib.sha256(BODY).hexdigest()
    assert event.event_type == "pull_request"
    assert event.status == DeliveryStatus.PROCESSING


async def test_complete_moves_the_row_to_its_final_status(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    await guard.claim("delivery-a", "pull_request", BODY)
    await guard.complete("delivery-a", DeliveryStatus.PROCESSED)

    status = await db_session.scalar(
        select(WebhookEvent.status).where(WebhookEvent.github_delivery_id == "delivery-a")
    )
    assert status == DeliveryStatus.PROCESSED


async def test_release_lets_a_retry_through(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    await guard.claim("delivery-a", "pull_request", BODY)
    await guard.release("delivery-a")

    assert await guard.claim("delivery-a", "pull_request", BODY) is True
    assert await count_events(db_session) == 1


async def test_separate_deliveries_are_independent(
    guard: WebhookIdempotencyGuard, db_session: AsyncSession
) -> None:
    assert await guard.claim("delivery-a", "pull_request", BODY) is True
    assert await guard.claim("delivery-b", "pull_request", BODY) is True

    assert await count_events(db_session) == 2
