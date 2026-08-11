from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.github.webhooks.issues import parse_issue_event
from shannon.services.item_sync import ItemSyncService, build_item_handler
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def logins(session: AsyncSession, role: ActorRole) -> list[str]:
    rows = await session.scalars(
        select(ItemAssignment.github_username).where(ItemAssignment.role_type == role)
    )
    return sorted(rows.all())


async def test_a_new_issue_creates_one_tracked_item(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    result = await issue_service.sync(issue_event("opened"))

    assert result is not None
    assert result.created is True

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_object_type is ObjectType.ISSUE
    assert item.github_object_id == payloads.ISSUE_ID
    assert item.github_object_number == 12
    assert item.title == "Threads are not locked when an issue closes"
    assert item.status is Status.NOT_REVIEWED


async def test_the_issue_thread_goes_in_the_issue_channel(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    """Pull requests are mapped to 99 and issues to 98, so this proves the mapping is honoured."""
    await issue_service.sync(issue_event("opened"))

    assert threads.created[0].channel_id == 98


async def test_priority_is_taken_from_the_labels(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened", labels=[{"name": "priority: high"}]))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.priority is Priority.HIGH


async def test_priority_follows_a_label_change(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened", labels=[{"name": "priority: high"}]))
    await issue_service.sync(issue_event("labeled", labels=[{"name": "low"}]))

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.priority is Priority.LOW


async def test_an_issue_with_no_priority_label_is_unset(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened", labels=[{"name": "bug"}]))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.priority is Priority.UNSET


async def test_an_existing_issue_updates_rather_than_duplicates(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    await issue_service.sync(issue_event("opened"))
    result = await issue_service.sync(issue_event("edited", title="A clearer title"))

    assert result is not None
    assert result.created is False
    assert await count(db_session, TrackedItem) == 1
    assert len(threads.created) == 1


async def test_the_same_payload_twice_changes_nothing(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("opened"))

    assert await count(db_session, TrackedItem) == 1
    assert await count(db_session, ItemAssignment) == 2
    assert len(threads.created) == 1


async def test_the_author_and_assignees_are_stored(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened"))

    assert await logins(db_session, ActorRole.AUTHOR) == ["octocat"]
    assert await logins(db_session, ActorRole.ASSIGNEE) == ["hubot"]


async def test_issues_never_store_reviewers(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    """GitHub issues have no reviewers, so that role should never appear for one."""
    await issue_service.sync(issue_event("opened"))

    assert await logins(db_session, ActorRole.REVIEWER) == []


async def test_reassignment_replaces_the_previous_assignees(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened"))

    await issue_service.sync(issue_event("assigned", assignees=[payloads.user("monalisa", 200)]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == ["monalisa"]


async def test_removing_every_assignee_leaves_none_behind(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
) -> None:
    await issue_service.sync(issue_event("opened"))

    await issue_service.sync(issue_event("assigned", assignees=[]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == []


async def test_an_issue_and_a_pull_request_can_share_a_number(
    registered: Repository,
    issue_service: ItemSyncService,
    sync_service: ItemSyncService,
    issue_event,
    pr_event,
    db_session: AsyncSession,
) -> None:
    """They are told apart by object type, which is what the unique constraint keys on."""
    await sync_service.sync(pr_event("opened"))
    await issue_service.sync(issue_event("opened", number=7))

    items = (await db_session.scalars(select(TrackedItem))).all()
    assert len(items) == 2
    assert {item.github_object_type for item in items} == {ObjectType.PR, ObjectType.ISSUE}


async def test_an_unregistered_repository_is_ignored(
    issue_service: ItemSyncService, issue_event, db_session: AsyncSession
) -> None:
    assert await issue_service.sync(issue_event("opened")) is None
    assert await count(db_session, TrackedItem) == 0


async def test_a_guild_without_an_issue_channel_falls_back_to_the_pull_request_one(
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """/register only maps pull requests, and issues should still appear somewhere."""
    from tests.support.db import register_repository

    await register_repository(db_session, channel_id=99)

    result = await issue_service.sync(issue_event("opened"))

    assert result is not None
    assert threads.created[0].channel_id == 99


async def test_an_explicit_issue_channel_wins_over_the_fallback(
    registered: Repository,
    issue_service: ItemSyncService,
    issue_event,
    threads: FakeThreadGateway,
) -> None:
    await issue_service.sync(issue_event("opened"))

    assert threads.created[0].channel_id == 98


async def test_a_repository_with_no_channels_at_all_is_ignored(
    issue_service: ItemSyncService,
    issue_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    from shannon.db.models import Repository as RepositoryModel

    db_session.add(
        RepositoryModel(
            github_repo_id=payloads.REPO_ID,
            repo_name=f"{payloads.OWNER}/{payloads.REPO}",
            repo_url="https://github.com/x/y",
            discord_guild_id=1,
        )
    )
    await db_session.commit()

    assert await issue_service.sync(issue_event("opened")) is None
    assert threads.created == []


async def test_the_webhook_handler_reports_processed_for_real_work(
    registered: Repository, issue_service: ItemSyncService
) -> None:
    handler = build_item_handler(issue_service, parse_issue_event)

    assert await handler("opened", payloads.issue_event("opened")) == "processed"


async def test_the_webhook_handler_ignores_an_out_of_scope_action(
    registered: Repository, issue_service: ItemSyncService
) -> None:
    handler = build_item_handler(issue_service, parse_issue_event)

    assert await handler("milestoned", payloads.issue_event("milestoned")) == "ignored"
