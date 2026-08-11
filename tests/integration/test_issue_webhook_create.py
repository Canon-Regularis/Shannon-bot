from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, TrackedItem, WebhookEvent
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import map_channel, register_repository
from tests.support.stack import build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest_asyncio.fixture
async def client(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    repository = await register_repository(db_session, guild_id=1, channel_id=99)
    await map_channel(db_session, repository, ObjectType.ISSUE, channel_id=98)
    container = build_stack(db_engine, threads=threads)
    async with build_http_client(container) as http_client:
        yield http_client


async def test_an_opened_issue_creates_a_thread(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(client, "issues", payloads.issue_event("opened"))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert len(threads.created) == 1
    assert threads.created[0].channel_id == 98


async def test_the_thread_is_named_after_the_issue(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(client, "issues", payloads.issue_event("opened"))

    assert threads.created[0].name == "#12 Threads are not locked when an issue closes"


async def test_the_thread_opens_with_the_full_metadata_block(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        client, "issues", payloads.issue_event("opened", labels=[{"name": "priority: high"}])
    )

    metadata = threads.metadata_of(threads.created[0].thread_id)
    assert "**Issue Name:** Threads are not locked when an issue closes" in metadata
    assert "**Type:** Issue" in metadata
    assert "**State:** Open" in metadata
    assert "**GitHub Link:** https://github.com/Canon-Regularis/Shannon-bot/issues/12" in metadata
    assert "**Author:** octocat" in metadata
    assert "**Assignees:** hubot" in metadata
    assert "**Status:** NOT_REVIEWED" in metadata
    assert "**Priority:** HIGH" in metadata
    assert "**Tags:** `priority: high`" in metadata
    assert "**Last Updated:**" in metadata
    assert "Reviewers" not in metadata


async def test_the_tracked_item_is_written(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await deliver(client, "issues", payloads.issue_event("opened"))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_object_type is ObjectType.ISSUE
    assert item.github_object_id == payloads.ISSUE_ID
    assert item.status is Status.NOT_REVIEWED
    assert item.priority is Priority.UNSET
    assert item.discord_thread_id == threads.created[0].thread_id
    assert item.discord_message_id is not None


async def test_the_author_and_assignees_are_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await deliver(client, "issues", payloads.issue_event("opened"))

    rows = (await db_session.scalars(select(ItemAssignment))).all()
    assert sorted((r.role_type, r.github_username) for r in rows) == [
        (ActorRole.ASSIGNEE, "hubot"),
        (ActorRole.AUTHOR, "octocat"),
    ]


async def test_the_assignee_is_pinged_in_the_new_thread(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(client, "issues", payloads.issue_event("opened"))

    assert len(threads.posts) == 1
    assert "hubot" in threads.posts[0][1]
    assert threads.posts[0][1].startswith("Assigned to")


async def test_a_linked_assignee_is_pinged_by_mention(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await UserLinkStore(db_session).link(guild_id=1, github_username="hubot", discord_user_id=4242)
    await db_session.commit()

    await deliver(client, "issues", payloads.issue_event("opened"))

    assert "<@4242>" in threads.posts[0][1]


async def test_the_delivery_is_logged(client: AsyncClient, db_session: AsyncSession) -> None:
    await deliver(client, "issues", payloads.issue_event("opened"), delivery="issue-1")

    event = await db_session.scalar(select(WebhookEvent))
    assert event is not None
    assert event.event_type == "issues"
    assert event.status == "PROCESSED"


async def test_the_same_delivery_twice_creates_one_thread(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    payload = payloads.issue_event("opened")
    first = await deliver(client, "issues", payload, delivery="same")
    second = await deliver(client, "issues", payload, delivery="same")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_a_redelivery_under_a_new_id_still_creates_one_thread(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    payload = payloads.issue_event("opened")
    await deliver(client, "issues", payload, delivery="one")
    await deliver(client, "issues", payload, delivery="two")

    assert len(threads.created) == 1
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_an_unsigned_delivery_never_reaches_the_database(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    response = await client.post(
        "/webhooks/github",
        content=json.dumps(payloads.issue_event("opened")),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "forged"},
    )

    assert response.status_code == 401
    assert threads.created == []
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 0


async def test_an_issue_from_another_repository_is_ignored(
    client: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    payload = payloads.issue_event("opened")
    payload["repository"]["id"] = 999999

    await deliver(client, "issues", payload)

    assert await client.outcome_of("delivery-1") == "ignored"
    assert threads.created == []


async def test_an_out_of_scope_action_is_ignored(
    client: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(client, "issues", payloads.issue_event("milestoned"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert threads.created == []
