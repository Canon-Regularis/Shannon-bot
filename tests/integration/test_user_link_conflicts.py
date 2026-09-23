from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.services.linking import UserLinkingService

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker[AsyncSession]) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker)


async def rows(session: AsyncSession) -> list[tuple[str, int]]:
    session.expunge_all()
    result = await session.scalars(select(UserLink).order_by(UserLink.id))
    return [(r.github_username, r.discord_user_id) for r in result.all()]


async def test_taking_over_a_github_name_already_linked_to_someone_else(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    """Two people connect their accounts, then one signs in as the other's.

    Both halves of the pairing are unique per guild, so the naive update collides with the row
    that already holds the name.
    """
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)
    await service.bind(guild_id=1, discord_user_id=200, login="bob", github_user_id=222)

    await service.bind(guild_id=1, discord_user_id=100, login="bob", github_user_id=222)

    assert await rows(db_session) == [("bob", 100)]


async def test_giving_a_discord_account_a_name_someone_else_holds(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)
    await service.bind(guild_id=1, discord_user_id=200, login="bob", github_user_id=222)

    await service.bind(guild_id=1, discord_user_id=200, login="alice", github_user_id=111)

    assert await rows(db_session) == [("alice", 200)]


async def test_relinking_the_same_pairing_is_harmless(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)

    assert await rows(db_session) == [("alice", 100)]


async def test_unrelated_pairings_are_left_alone(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)
    await service.bind(guild_id=1, discord_user_id=200, login="bob", github_user_id=222)

    await service.bind(guild_id=1, discord_user_id=300, login="carol", github_user_id=333)

    assert await rows(db_session) == [("alice", 100), ("bob", 200), ("carol", 300)]


async def test_a_conflict_in_another_guild_is_not_touched(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.bind(guild_id=1, discord_user_id=100, login="alice", github_user_id=111)
    await service.bind(guild_id=2, discord_user_id=100, login="alice", github_user_id=111)

    await service.bind(guild_id=1, discord_user_id=100, login="bob", github_user_id=222)

    assert await db_session.scalar(select(func.count()).select_from(UserLink)) == 2


async def test_two_links_landing_together_do_not_raise_at_whoever_lost(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    """Two people signing in as one GitHub account at once, or one callback arriving twice.

    Both halves of a link are unique within a guild, so the store clears anything holding either
    half and writes the pairing fresh. Overlapping, they all find nothing to clear and all
    insert, and a loser must not surface a raw database error over a link that did commit.

    Eight callers because two never reaches the interesting case: with three or more, retries
    collide with each other rather than with the original winner.
    """
    results = await asyncio.gather(
        *(
            service.bind(guild_id=1, discord_user_id=who, login="octocat", github_user_id=583231)
            for who in range(500, 508)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"a concurrent bind raised: {failures}"
    # One name, one holder: whoever committed last, which is what taking over a name means.
    assert await db_session.scalar(select(func.count()).select_from(UserLink)) == 1
