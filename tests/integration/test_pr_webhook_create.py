from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, TrackedItem, WebhookEvent
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType, Status
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    # No issue channel: this file is only about pull requests, and mapping one would hide the
    # case the fallback exists for.
    async with registered_stack(db_engine, db_session, threads, issues_channel=None) as client:
        yield client


async def test_an_opened_pull_request_creates_a_thread(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert len(threads.created) == 1
    assert threads.created[0].channel_id == 99


async def test_the_thread_is_named_after_the_pull_request(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    assert threads.created[0].name == "#7 Add the webhook endpoint"


async def test_the_thread_opens_with_the_full_metadata_block(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    metadata = threads.metadata_of(threads.created[0].thread_id)
    assert "**PR Name:** Add the webhook endpoint" in metadata
    assert "**Type:** PR" in metadata
    assert "**State:** Open" in metadata
    assert "**GitHub Link:** https://github.com/Canon-Regularis/Shannon-bot/pull/7" in metadata
    assert "**Author:** octocat" in metadata
    assert "**Assignees:** hubot" in metadata
    assert "**Reviewers:** monalisa" in metadata
    assert "**Status:** NOT_REVIEWED" in metadata
    assert "**Priority:** UNSET" in metadata
    assert "**Tags:** `backend`" in metadata
    assert "**Last Updated:**" in metadata


async def test_the_tracked_item_is_written(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_object_type == ObjectType.PR
    assert item.github_object_id == payloads.PR_ID
    assert item.status == Status.NOT_REVIEWED
    assert item.discord_thread_id == threads.created[0].thread_id
    assert item.discord_message_id is not None


async def test_everyone_on_the_pull_request_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    rows = (await db_session.scalars(select(ItemAssignment))).all()
    assert sorted((r.role_type, r.github_username) for r in rows) == [
        (ActorRole.ASSIGNEE, "hubot"),
        (ActorRole.AUTHOR, "octocat"),
        (ActorRole.REVIEWER, "monalisa"),
    ]


async def test_the_reviewer_is_pinged_in_the_new_thread(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    assert len(threads.posts) == 1
    assert "monalisa" in threads.posts[0][1]


async def test_a_linked_reviewer_is_pinged_by_mention(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=777
    )
    await db_session.commit()

    await deliver(client, "pull_request", payloads.pull_request_event("opened"))

    assert "<@777>" in threads.posts[0][1]


async def test_the_delivery_is_logged(client: AsyncClient, db_session: AsyncSession) -> None:
    await deliver(client, "pull_request", payloads.pull_request_event("opened"), delivery="abc")

    event = await db_session.scalar(select(WebhookEvent))
    assert event is not None
    assert event.github_delivery_id == "abc"
    assert event.event_type == "pull_request"
    assert event.status == "PROCESSED"


async def test_the_same_delivery_twice_creates_one_thread(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    payload = payloads.pull_request_event("opened")
    first = await deliver(client, "pull_request", payload, delivery="same")
    second = await deliver(client, "pull_request", payload, delivery="same")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_a_redelivery_under_a_new_id_still_creates_one_thread(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    """Even past the delivery guard, the tracked item is what stops a second thread."""
    payload = payloads.pull_request_event("opened")
    await deliver(client, "pull_request", payload, delivery="one")
    await deliver(client, "pull_request", payload, delivery="two")

    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_an_unsigned_delivery_never_reaches_the_database(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    import json

    response = await client.post(
        "/webhooks/github",
        content=json.dumps(payloads.pull_request_event("opened")),
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "forged"},
    )

    assert response.status_code == 401
    assert threads.created == []
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WebhookEvent)) == 0


async def test_a_pull_request_from_another_repository_is_ignored(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    payload = payloads.pull_request_event("opened")
    payload["repository"]["id"] = 999999

    await deliver(client, "pull_request", payload)

    assert await client.outcome_of("delivery-1") == "ignored"
    assert threads.created == []
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 0
