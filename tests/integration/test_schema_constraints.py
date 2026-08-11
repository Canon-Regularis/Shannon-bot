from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping, ItemAssignment, Repository, TrackedItem, WebhookEvent
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status

pytestmark = pytest.mark.integration


def make_repository(*, guild_id: int = 1, repo_id: int = 100) -> Repository:
    return Repository(
        github_repo_id=repo_id,
        repo_name="Canon-Regularis/Shannon-bot",
        repo_url="https://github.com/Canon-Regularis/Shannon-bot",
        discord_guild_id=guild_id,
    )


def make_tracked_item(repository_id: int, *, object_id: int = 555) -> TrackedItem:
    return TrackedItem(
        repository_id=repository_id,
        github_object_id=object_id,
        github_object_type=ObjectType.PR,
        github_object_number=7,
        github_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        title="Add webhook endpoint",
        github_state="open",
        status=Status.NOT_REVIEWED,
        priority=Priority.UNSET,
    )


async def test_one_repository_per_guild(db_session: AsyncSession) -> None:
    db_session.add(make_repository(guild_id=1, repo_id=100))
    await db_session.commit()

    db_session.add(make_repository(guild_id=1, repo_id=200))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_repository_bound_to_one_guild(db_session: AsyncSession) -> None:
    db_session.add(make_repository(guild_id=1, repo_id=100))
    await db_session.commit()

    db_session.add(make_repository(guild_id=2, repo_id=100))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_duplicate_tracked_item_is_rejected(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id))
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_same_object_id_allowed_for_different_object_types(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(make_tracked_item(repository.id, object_id=555))
    issue = make_tracked_item(repository.id, object_id=555)
    issue.github_object_type = ObjectType.ISSUE
    db_session.add(issue)

    await db_session.commit()


async def test_one_channel_per_object_type(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()

    db_session.add(
        ChannelMapping(
            repository_id=repository.id, object_type=ObjectType.PR, discord_channel_id=42
        )
    )
    await db_session.commit()

    db_session.add(
        ChannelMapping(
            repository_id=repository.id, object_type=ObjectType.PR, discord_channel_id=43
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_assignment_is_unique_per_user_and_role(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()
    item = make_tracked_item(repository.id)
    db_session.add(item)
    await db_session.commit()

    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.REVIEWER
        )
    )
    await db_session.commit()

    # Same person in a different role is legitimate.
    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.ASSIGNEE
        )
    )
    await db_session.commit()

    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.REVIEWER
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_delivery_id_is_unique(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookEvent(
            github_delivery_id="abc-123",
            event_type="pull_request",
            payload_hash="0" * 64,
            status="PROCESSED",
        )
    )
    await db_session.commit()

    db_session.add(
        WebhookEvent(
            github_delivery_id="abc-123",
            event_type="pull_request",
            payload_hash="1" * 64,
            status="PROCESSED",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_repository_cascades(db_session: AsyncSession) -> None:
    repository = make_repository()
    db_session.add(repository)
    await db_session.commit()
    item = make_tracked_item(repository.id)
    db_session.add(item)
    await db_session.commit()
    db_session.add(
        ItemAssignment(
            tracked_item_id=item.id, github_username="octocat", role_type=ActorRole.AUTHOR
        )
    )
    await db_session.commit()

    await db_session.delete(repository)
    await db_session.commit()
    db_session.expunge_all()

    assert await db_session.get(TrackedItem, item.id) is None
