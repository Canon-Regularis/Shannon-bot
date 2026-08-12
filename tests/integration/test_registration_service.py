from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ChannelMapping, Repository
from shannon.domain.enums import ObjectType
from shannon.domain.errors import DuplicateRegistrationError, UnparseableLinkError
from shannon.domain.models import RepositorySnapshot
from shannon.github.errors import GitHubNotFoundError
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.item_sync import ItemSyncService
from shannon.services.policies import PullRequestPolicy
from shannon.services.registration import RepositoryRegistrationService
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

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


class TestARepositoryRenamedOnGitHub:
    """Webhooks find a repository by its numeric id, but /pr compares the link by name."""

    async def test_the_stored_name_follows_the_rename(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        service = ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())
        # Read before expiring, or the attribute reload would be sync IO in an async test.
        repository_id = registered.id

        await service.sync(_renamed_to("Shannon"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None
        assert stored.repo_name == "Canon-Regularis/Shannon"
        assert stored.repo_url == "https://github.com/Canon-Regularis/Shannon"

    async def test_an_unchanged_name_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())
        repository_id, before = registered.id, registered.updated_at

        await service.sync(pr_event("edited"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None and stored.updated_at == before

    async def test_a_stale_delivery_does_not_put_the_old_name_back(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Every payload carries the name as it was when GitHub sent it, late ones included."""
        service = ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())
        repository_id = registered.id
        await service.sync(_renamed_to("Shannon", at="2026-08-12T12:00:00Z"))

        await service.sync(_at_the_old_name(at="2026-08-01T12:00:00Z"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None
        assert stored.repo_name == "Canon-Regularis/Shannon"


def _renamed_to(name: str, *, at: str | None = None):
    """The same repository, under the name GitHub reports after a rename."""
    payload = payloads.pull_request_event("edited", **({"updated_at": at} if at else {}))
    payload["repository"]["name"] = name
    payload["repository"]["full_name"] = f"Canon-Regularis/{name}"
    payload["repository"]["html_url"] = f"https://github.com/Canon-Regularis/{name}"
    snapshot = parse_pull_request_event("edited", payload)
    assert snapshot is not None
    return snapshot


def _at_the_old_name(*, at: str):
    payload = payloads.pull_request_event("edited", updated_at=at)
    snapshot = parse_pull_request_event("edited", payload)
    assert snapshot is not None
    return snapshot
