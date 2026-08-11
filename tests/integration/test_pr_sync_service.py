from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.item_sync import ItemSyncService, SyncOutcome, build_item_handler
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


async def test_a_new_pull_request_creates_one_tracked_item(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    result = await sync_service.sync(pr_event("opened"))

    assert result is not None
    assert result.created is True

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.repository_id == registered.id
    assert item.github_object_type == ObjectType.PR
    assert item.github_object_id == payloads.PR_ID
    assert item.github_object_number == 7
    assert item.github_url == "https://github.com/Canon-Regularis/Shannon-bot/pull/7"
    assert item.title == "Add the webhook endpoint"


async def test_a_new_pull_request_starts_not_reviewed_and_unset(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.status == Status.NOT_REVIEWED
    assert item.priority == Priority.UNSET


async def test_the_thread_ids_are_written_back(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    result = await sync_service.sync(pr_event("opened"))

    item = await db_session.scalar(select(TrackedItem))
    assert result is not None and item is not None
    assert item.discord_thread_id == result.thread_id
    assert item.discord_message_id == result.message_id


async def test_an_existing_pull_request_updates_rather_than_duplicates(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))
    result = await sync_service.sync(pr_event("edited", title="A better title"))

    assert result is not None
    assert result.created is False
    assert await count(db_session, TrackedItem) == 1

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.title == "A better title"


async def test_the_discord_thread_id_survives_an_update(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    threads: FakeThreadGateway,
) -> None:
    first = await sync_service.sync(pr_event("opened"))
    second = await sync_service.sync(pr_event("edited", title="Renamed"))

    assert first is not None and second is not None
    assert second.thread_id == first.thread_id
    assert second.message_id == first.message_id
    assert len(threads.created) == 1


async def test_the_same_payload_twice_changes_nothing(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """A webhook that slips past the delivery guard still must not double anything up."""
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("opened"))

    assert await count(db_session, TrackedItem) == 1
    assert await count(db_session, ItemAssignment) == 3
    assert len(threads.created) == 1


async def test_author_assignees_and_reviewers_are_all_stored(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    assert await logins(db_session, ActorRole.AUTHOR) == ["octocat"]
    assert await logins(db_session, ActorRole.ASSIGNEE) == ["hubot"]
    assert await logins(db_session, ActorRole.REVIEWER) == ["monalisa"]


async def test_reassignment_replaces_the_previous_assignees(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    await sync_service.sync(pr_event("assigned", assignees=[payloads.user("octocat", 583231)]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == ["octocat"]


async def test_removing_every_assignee_leaves_none_behind(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    await sync_service.sync(pr_event("assigned", assignees=[]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == []


async def test_a_new_reviewer_is_added_without_dropping_the_first(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    await sync_service.sync(
        pr_event(
            "review_requested",
            requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
        )
    )

    assert await logins(db_session, ActorRole.REVIEWER) == ["hubot", "monalisa"]


async def test_logins_are_stored_lowercased(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened", user=payloads.user("OctoCat", 583231)))

    assert await logins(db_session, ActorRole.AUTHOR) == ["octocat"]


async def test_a_login_that_only_changes_case_is_not_re_added(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("edited", assignees=[payloads.user("HUBOT", 100)]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == ["hubot"]


async def test_a_label_change_reaches_the_thread(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    threads: FakeThreadGateway,
) -> None:
    result = await sync_service.sync(pr_event("opened"))
    assert result is not None
    assert "`backend`" in threads.metadata_of(result.thread_id)

    await sync_service.sync(
        pr_event("labeled", labels=[{"name": "backend"}, {"name": "needs docs"}])
    )

    assert "`needs docs`" in threads.metadata_of(result.thread_id)


async def test_a_closed_pull_request_updates_the_stored_state(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))
    await sync_service.sync(pr_event("closed", state="closed"))

    item = await db_session.scalar(select(TrackedItem))
    assert item is not None
    assert item.github_state == "closed"


async def test_an_unregistered_repository_is_ignored(
    sync_service: ItemSyncService, pr_event, db_session: AsyncSession
) -> None:
    result = await sync_service.sync(pr_event("opened"))

    assert result.outcome is SyncOutcome.NOT_TRACKED
    assert await count(db_session, TrackedItem) == 0


async def test_a_repository_without_a_channel_mapping_is_ignored(
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    db_session.add(
        Repository(
            github_repo_id=payloads.REPO_ID,
            repo_name=f"{payloads.OWNER}/{payloads.REPO}",
            repo_url="https://github.com/x/y",
            discord_guild_id=1,
        )
    )
    await db_session.commit()

    assert (await sync_service.sync(pr_event("opened"))).outcome is SyncOutcome.NOT_TRACKED
    assert await count(db_session, TrackedItem) == 0


async def test_the_webhook_handler_reports_processed_for_real_work(
    registered: Repository, sync_service: ItemSyncService
) -> None:
    handler = build_item_handler(sync_service, parse_pull_request_event)

    outcome = await handler("opened", payloads.pull_request_event("opened"))

    assert outcome == "processed"


async def test_the_webhook_handler_ignores_an_out_of_scope_action(
    registered: Repository, sync_service: ItemSyncService
) -> None:
    handler = build_item_handler(sync_service, parse_pull_request_event)

    outcome = await handler("synchronize", payloads.pull_request_event("synchronize"))

    assert outcome == "ignored"


async def test_the_webhook_handler_ignores_an_unregistered_repository(
    sync_service: ItemSyncService,
) -> None:
    handler = build_item_handler(sync_service, parse_pull_request_event)

    outcome = await handler("opened", payloads.pull_request_event("opened"))

    assert outcome == "ignored"


async def test_removing_a_reviewer_clears_them_from_the_thread(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """The removed reviewer is named in the payload, and must not be put straight back."""
    result = await sync_service.sync(pr_event("opened"))
    assert result is not None

    payload = payloads.pull_request_event("review_request_removed", requested_reviewers=[])
    payload["requested_reviewer"] = payloads.user("monalisa", 200)
    snapshot = parse_pull_request_event("review_request_removed", payload)
    assert snapshot is not None
    await sync_service.sync(snapshot)

    assert await logins(db_session, ActorRole.REVIEWER) == []
    assert "**Reviewers:** None" in threads.metadata_of(result.thread_id)


async def test_removing_an_assignee_clears_them_from_the_thread(
    registered: Repository,
    sync_service: ItemSyncService,
    pr_event,
    db_session: AsyncSession,
) -> None:
    await sync_service.sync(pr_event("opened"))

    await sync_service.sync(pr_event("unassigned", assignees=[]))

    assert await logins(db_session, ActorRole.ASSIGNEE) == []
