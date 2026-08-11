from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.db.stores.user_links import UserLinkStore
from shannon.services.linking import InvalidGitHubUsernameError, UserLinkingService

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker)


async def count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(UserLink)) or 0


async def test_a_link_is_stored_lowercased(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    assert (
        await service.link(guild_id=1, github_username="OctoCat", discord_user_id=42) == "OctoCat"
    )

    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "octocat"
    assert row.discord_user_id == 42


async def test_an_at_prefix_is_stripped(service: UserLinkingService) -> None:
    assert (
        await service.link(guild_id=1, github_username="@octocat", discord_user_id=42) == "octocat"
    )


@pytest.mark.parametrize("username", ["not a name", "", "  ", "-leading", "a" * 40])
async def test_a_bad_username_is_refused(service: UserLinkingService, username: str) -> None:
    with pytest.raises(InvalidGitHubUsernameError):
        await service.link(guild_id=1, github_username=username, discord_user_id=42)


async def test_relinking_the_same_discord_account_replaces_the_github_name(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="monalisa", discord_user_id=42)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "monalisa"


async def test_moving_a_github_name_to_another_account_replaces_the_row(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="octocat", discord_user_id=99)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.discord_user_id == 99


async def test_links_are_scoped_to_a_guild(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=2, github_username="octocat", discord_user_id=77)

    assert await count(db_session) == 2


async def test_resolving_many_ignores_case_and_skips_unknowns(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)

    resolved = await UserLinkStore(db_session).resolve_many(
        guild_id=1, github_usernames=["OctoCat", "nobody"]
    )

    assert resolved == {"octocat": 42}


async def test_resolving_nothing_asks_the_database_for_nothing(
    db_session: AsyncSession,
) -> None:
    assert await UserLinkStore(db_session).resolve_many(guild_id=1, github_usernames=[]) == {}
