from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.services.linking import UserLinkingService

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker)


async def rows(session: AsyncSession) -> list[tuple[str, int]]:
    session.expunge_all()
    result = await session.scalars(select(UserLink).order_by(UserLink.id))
    return [(r.github_username, r.discord_user_id) for r in result.all()]


async def test_taking_over_a_github_name_already_linked_to_someone_else(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    """Two people link themselves, then one claims the other's GitHub name.

    Both halves of the pairing are unique per guild, so the naive update collides with the row
    that already holds the name.
    """
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)
    await service.link(guild_id=1, github_username="bob", discord_user_id=200)

    await service.link(guild_id=1, github_username="bob", discord_user_id=100)

    assert await rows(db_session) == [("bob", 100)]


async def test_giving_a_discord_account_a_name_someone_else_holds(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)
    await service.link(guild_id=1, github_username="bob", discord_user_id=200)

    await service.link(guild_id=1, github_username="alice", discord_user_id=200)

    assert await rows(db_session) == [("alice", 200)]


async def test_relinking_the_same_pairing_is_harmless(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)

    assert await rows(db_session) == [("alice", 100)]


async def test_unrelated_pairings_are_left_alone(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)
    await service.link(guild_id=1, github_username="bob", discord_user_id=200)

    await service.link(guild_id=1, github_username="carol", discord_user_id=300)

    assert await rows(db_session) == [("alice", 100), ("bob", 200), ("carol", 300)]


async def test_a_conflict_in_another_guild_is_not_touched(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="alice", discord_user_id=100)
    await service.link(guild_id=2, github_username="alice", discord_user_id=100)

    await service.link(guild_id=1, github_username="bob", discord_user_id=100)

    assert await db_session.scalar(select(func.count()).select_from(UserLink)) == 2
