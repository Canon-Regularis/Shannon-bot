from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ChannelMapping, Repository
from shannon.domain.enums import ObjectType
from shannon.domain.errors import DuplicateRegistrationError, UnparseableLinkError
from shannon.domain.models import RepositorySnapshot
from shannon.github.errors import GitHubNotFoundError
from shannon.services.registration import RepositoryRegistrationService
from tests.fakes.github import FakeGitHubClient

pytestmark = pytest.mark.integration

REPO_LINK = "https://github.com/Canon-Regularis/Shannon-bot"
SNAPSHOT = RepositorySnapshot(
    github_repo_id=1255504909,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url=REPO_LINK,
)


@pytest.fixture
def github() -> FakeGitHubClient:
    return FakeGitHubClient(repositories={"canon-regularis/shannon-bot": SNAPSHOT})


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker, github: FakeGitHubClient
) -> RepositoryRegistrationService:
    return RepositoryRegistrationService(db_sessionmaker, github)


async def test_registration_stores_repository_and_pr_channel(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    result = await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    assert result.full_name == "Canon-Regularis/Shannon-bot"
    assert result.pr_channel_id == 10

    repository = await db_session.scalar(select(Repository))
    assert repository is not None
    assert repository.github_repo_id == SNAPSHOT.github_repo_id
    assert repository.repo_name == "Canon-Regularis/Shannon-bot"
    assert repository.discord_guild_id == 1

    mapping = await db_session.scalar(select(ChannelMapping))
    assert mapping is not None
    assert mapping.object_type == ObjectType.PR
    assert mapping.discord_channel_id == 10


async def test_a_deep_link_still_registers_the_repository(
    service: RepositoryRegistrationService,
) -> None:
    result = await service.register(guild_id=1, channel_id=10, link=f"{REPO_LINK}/pull/7/files")

    assert result.full_name == "Canon-Regularis/Shannon-bot"


async def test_second_registration_in_the_same_guild_is_rejected(
    service: RepositoryRegistrationService, github: FakeGitHubClient, db_session: AsyncSession
) -> None:
    github.repositories["other/repo"] = RepositorySnapshot(
        github_repo_id=999, owner="other", name="repo", html_url="https://github.com/other/repo"
    )
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError, match="already registered to"):
        await service.register(guild_id=1, channel_id=11, link="https://github.com/other/repo")

    assert len((await db_session.scalars(select(Repository))).all()) == 1


async def test_same_repository_in_a_second_guild_is_rejected(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError, match="already registered to another server"):
        await service.register(guild_id=2, channel_id=20, link=REPO_LINK)

    assert len((await db_session.scalars(select(Repository))).all()) == 1


async def test_unknown_repository_is_rejected(service: RepositoryRegistrationService) -> None:
    with pytest.raises(GitHubNotFoundError):
        await service.register(guild_id=1, channel_id=10, link="https://github.com/who/what")


async def test_invalid_link_never_reaches_github(
    service: RepositoryRegistrationService, github: FakeGitHubClient
) -> None:
    with pytest.raises(UnparseableLinkError):
        await service.register(guild_id=1, channel_id=10, link="https://gitlab.com/owner/repo")

    assert github.repository_calls == []


async def test_a_rejected_registration_leaves_no_rows_behind(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError):
        await service.register(guild_id=2, channel_id=20, link=REPO_LINK)

    mappings = (await db_session.scalars(select(ChannelMapping))).all()
    assert len(mappings) == 1
