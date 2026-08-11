from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping, Repository
from shannon.domain.enums import ObjectType
from tests.support import github_payloads as payloads


async def register_repository(
    session: AsyncSession,
    *,
    guild_id: int = 1,
    channel_id: int = 99,
    github_repo_id: int = payloads.REPO_ID,
    repo_name: str = f"{payloads.OWNER}/{payloads.REPO}",
) -> Repository:
    """The state /register leaves behind, without going through GitHub."""
    repository = Repository(
        github_repo_id=github_repo_id,
        repo_name=repo_name,
        repo_url=f"https://github.com/{repo_name}",
        discord_guild_id=guild_id,
    )
    session.add(repository)
    await session.commit()

    session.add(
        ChannelMapping(
            repository_id=repository.id,
            object_type=ObjectType.PR,
            discord_channel_id=channel_id,
        )
    )
    await session.commit()
    return repository


async def map_channel(
    session: AsyncSession,
    repository: Repository,
    object_type: ObjectType,
    *,
    channel_id: int,
) -> None:
    """The state /set_channel leaves behind."""
    session.add(
        ChannelMapping(
            repository_id=repository.id,
            object_type=object_type,
            discord_channel_id=channel_id,
        )
    )
    await session.commit()
