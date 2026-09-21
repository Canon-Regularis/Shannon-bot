from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    private: bool | None = False,
) -> Repository:
    """The state /register leaves behind, without going through GitHub.

    `private` matches what the payload helpers say, so a sync driven by one of them learns nothing
    new about the repository and leaves the row alone. Registration records the visibility, and
    every delivery afterwards keeps it current, so a fixture that left it unknown would have every
    test's first sync writing the row for a reason that has nothing to do with the test.
    """
    repository = Repository(
        github_repo_id=github_repo_id,
        repo_name=repo_name,
        repo_url=f"https://github.com/{repo_name}",
        discord_guild_id=guild_id,
        private=private,
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


async def blocked_on_a_row(
    sessionmaker: async_sessionmaker[AsyncSession], task: asyncio.Task[object]
) -> None:
    """Wait until the task is genuinely waiting on a lock somebody else holds.

    Sleeping a fixed moment instead is what these used to do, and on a loaded machine the task
    had not reached the database at all: the holder committed first, the sync found the row
    where it looks for it, and the test passed having exercised the other path entirely. It
    passed on its own and stopped covering the branch it was written for in a full run, which is
    the worst way for a race test to be wrong.

    Asked of PostgreSQL rather than guessed at. A backend waiting on a lock says so.
    """
    for _ in range(200):
        await asyncio.sleep(0.05)
        if task.done():
            break
        async with sessionmaker() as watcher:
            waiting = await watcher.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
        if waiting:
            return
    raise AssertionError("nothing ever blocked, so this proves nothing")
