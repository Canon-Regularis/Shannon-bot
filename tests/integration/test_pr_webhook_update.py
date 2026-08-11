from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, TrackedItem
from shannon.domain.enums import ActorRole
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import register_repository
from tests.support.stack import build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    """A guild with the pull request already synced once, which is the state updates arrive in."""
    await register_repository(db_session, guild_id=1, channel_id=99)
    container = build_stack(db_engine, threads=threads)
    async with build_http_client(container) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="d0"
        )
        yield http_client


async def thread_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(TrackedItem)) or 0


async def test_an_edit_updates_the_existing_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("edited", title="Now with signature checks"),
        delivery="d1",
    )

    assert response.json()["status"] == "accepted"
    assert len(threads.created) == 1
    metadata = threads.metadata_of(threads.created[0].thread_id)
    assert "**PR Name:** Now with signature checks" in metadata


async def test_a_title_change_renames_the_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("edited", title="Now with signature checks"),
        delivery="d1",
    )

    assert threads.renames == [(threads.created[0].thread_id, "#7 Now with signature checks")]


async def test_an_unchanged_title_does_not_rename(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "pull_request", payloads.pull_request_event("edited"), delivery="d1")

    assert threads.renames == []


async def test_a_new_label_reaches_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event(
            "labeled", labels=[{"name": "backend"}, {"name": "needs review"}]
        ),
        delivery="d1",
    )

    metadata = threads.metadata_of(threads.created[0].thread_id)
    assert "**Tags:** `backend`, `needs review`" in metadata


async def test_a_removed_label_disappears_from_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("labeled", labels=[]),
        delivery="d1",
    )

    assert "**Tags:** None" in threads.metadata_of(threads.created[0].thread_id)


async def test_a_reassignment_reaches_the_metadata_and_the_database(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("assigned", assignees=[payloads.user("octocat", 583231)]),
        delivery="d1",
    )

    assert "**Assignees:** octocat" in threads.metadata_of(threads.created[0].thread_id)

    rows = await db_session.scalars(
        select(ItemAssignment.github_username).where(ItemAssignment.role_type == ActorRole.ASSIGNEE)
    )
    assert sorted(rows.all()) == ["octocat"]


async def test_a_review_request_reaches_the_metadata(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    payload = payloads.pull_request_event(
        "review_requested",
        requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
    )

    await deliver(tracked, "pull_request", payload, delivery="d1")

    assert "**Assignees:** hubot" in threads.metadata_of(threads.created[0].thread_id)
    assert "**Reviewers:** monalisa, hubot" in threads.metadata_of(threads.created[0].thread_id)


async def test_a_review_request_pings_only_the_new_reviewer(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    payload = payloads.pull_request_event(
        "review_requested",
        requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
    )

    await deliver(tracked, "pull_request", payload, delivery="d1")

    assert len(threads.posts) == 2
    assert "hubot" in threads.posts[1][1]
    assert "monalisa" not in threads.posts[1][1]


async def test_no_update_ever_opens_a_second_thread(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    for index, action in enumerate(("edited", "labeled", "assigned", "review_requested")):
        await deliver(
            tracked, "pull_request", payloads.pull_request_event(action), delivery=f"d{index + 1}"
        )

    assert len(threads.created) == 1
    assert await thread_count(db_session) == 1


async def test_the_metadata_message_is_edited_rather_than_reposted(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    before = await db_session.scalar(select(TrackedItem.discord_message_id))

    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("edited", title="Edited"),
        delivery="d1",
    )
    db_session.expunge_all()
    after = await db_session.scalar(select(TrackedItem.discord_message_id))

    assert before is not None
    assert after == before


async def test_a_closed_update_reaches_the_existing_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(
        tracked,
        "pull_request",
        payloads.pull_request_event("closed", state="closed"),
        delivery="d1",
    )

    assert len(threads.created) == 1
    assert "**State:** Closed" in threads.metadata_of(threads.created[0].thread_id)


async def test_a_repeated_update_delivery_is_dropped(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    payload = payloads.pull_request_event("edited", title="Edited once")
    first = await deliver(tracked, "pull_request", payload, delivery="d1")
    second = await deliver(tracked, "pull_request", payload, delivery="d1")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.created) == 1
