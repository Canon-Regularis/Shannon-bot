from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ItemAssignment, Repository
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole
from shannon.services.item_sync import ItemSyncService
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration


async def test_a_requested_reviewer_is_pinged_once(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    result = await notifying_sync_service.sync(pr_event("opened"))

    assert result is not None
    assert result.notified == ("monalisa",)
    assert len(threads.posts) == 1
    assert "monalisa" in threads.posts[0][1]


async def test_a_second_webhook_does_not_ping_again(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))
    result = await notifying_sync_service.sync(pr_event("edited", title="Renamed"))

    assert result is not None
    assert result.notified == ()
    assert len(threads.posts) == 1


async def test_a_newly_added_reviewer_is_pinged_but_the_first_is_not(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))

    result = await notifying_sync_service.sync(
        pr_event(
            "review_requested",
            requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
        )
    )

    assert result is not None
    assert result.notified == ("hubot",)
    assert len(threads.posts) == 2
    assert "hubot" in threads.posts[1][1]
    assert "monalisa" not in threads.posts[1][1]


async def test_a_linked_reviewer_is_pinged_by_mention(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="MonaLisa", discord_user_id=555
    )
    await db_session.commit()

    await notifying_sync_service.sync(pr_event("opened"))

    assert "<@555>" in threads.posts[0][1]


async def test_an_unlinked_reviewer_is_named_in_plain_text(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))

    assert "monalisa" in threads.posts[0][1]
    assert "<@" not in threads.posts[0][1]


async def test_a_linked_reviewer_appears_as_a_mention_in_the_metadata(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", discord_user_id=555
    )
    await db_session.commit()

    result = await notifying_sync_service.sync(pr_event("opened"))

    assert result is not None
    assert "**Reviewers:** <@555>" in threads.metadata_of(result.thread_id)


async def test_notification_stamps_the_assignment_row(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", discord_user_id=555
    )
    await db_session.commit()

    await notifying_sync_service.sync(pr_event("opened"))

    row = await db_session.scalar(
        select(ItemAssignment).where(ItemAssignment.role_type == ActorRole.REVIEWER)
    )
    assert row is not None
    assert row.notified_at is not None
    assert row.discord_user_id == 555


async def test_a_removed_and_re_requested_reviewer_is_pinged_again(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await notifying_sync_service.sync(pr_event("opened"))
    await notifying_sync_service.sync(pr_event("edited", requested_reviewers=[]))

    result = await notifying_sync_service.sync(
        pr_event("review_requested", requested_reviewers=[payloads.user("monalisa", 200)])
    )

    assert result is not None
    assert result.notified == ("monalisa",)
    assert len(threads.posts) == 2


async def test_a_pull_request_with_no_reviewers_pings_nobody(
    registered: Repository,
    notifying_sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    result = await notifying_sync_service.sync(pr_event("opened", requested_reviewers=[]))

    assert result is not None
    assert result.notified == ()
    assert threads.posts == []
