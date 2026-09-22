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


NOTHING_BLOCKED = "nothing ever blocked, so this proves nothing"

# Thirty rather than ten. Nothing is waited for that is not already happening, so this only
# decides how long a loaded runner is given before the suite calls it a failure, and a shared
# CI runner is slower than anything this was timed against.
WAIT_FOR_A_LOCK_SECONDS = 30.0


async def blocked_on_a_row(
    sessionmaker: async_sessionmaker[AsyncSession],
    task: asyncio.Task[object],
    *,
    because: str = NOTHING_BLOCKED,
    timeout: float = WAIT_FOR_A_LOCK_SECONDS,
) -> None:
    """Wait until the task is genuinely waiting on a lock somebody else holds.

    Sleeping a fixed moment instead is what these used to do, and on a loaded machine the task
    had not reached the database at all: the holder committed first, the sync found the row
    where it looks for it, and the test passed having exercised the other path entirely. It
    passed on its own and stopped covering the branch it was written for in a full run, which is
    the worst way for a race test to be wrong.

    Asked of PostgreSQL rather than guessed at. A backend waiting on a lock says so.

    The database is asked before the task is. The other way round, a task that blocked and then
    finished inside one poll is reported as never having blocked.

    A task that finished without ever blocking has its result read, which re-raises whatever it
    hit with its own traceback. Polling alone cannot tell "has not got there yet" apart from "is
    never going to": the second used to spin out the whole timeout and fail pointing at the
    sleep, while the exception that explained it sat unretrieved on the task. That happened on CI
    and the log said nothing anybody could act on.
    """
    try:
        async with asyncio.timeout(timeout), sessionmaker() as watcher:
            while True:
                if await _waiting_on_a_lock(watcher):
                    return
                if task.done():
                    task.result()
                    raise AssertionError(because)
                await asyncio.sleep(0.02)
    except TimeoutError:
        await _settled(task)
        raise AssertionError(f"{because} (nothing was waiting after {timeout}s)") from None
    except BaseException:
        await _settled(task)
        raise


async def _waiting_on_a_lock(watcher: AsyncSession) -> bool:
    """Whether PostgreSQL says any backend on this database is stuck behind a lock.

    The snapshot is cleared first, and that is the whole reason this works. `pg_stat_activity`
    is read through the statistics snapshot, which `stats_fetch_consistency` keeps cached for
    the length of the transaction: a watcher that reads "nobody is waiting" once goes on reading
    it however many backends pile up afterwards, and the wait below runs to its timeout every
    time. Verified against PostgreSQL 17 - a session reading 0, then re-reading after another
    backend has genuinely blocked, still answers 0 until this is called.
    """
    await watcher.execute(text("SELECT pg_stat_clear_snapshot()"))
    found = await watcher.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND datname = current_database()"
        )
    )
    return bool(found)


async def _settled(task: asyncio.Task[object]) -> None:
    """Finish with the task before giving up on it.

    Every caller holds the other writer's session open in an `async with` around this. A task
    left mid-statement has that session closed underneath it, so whatever actually went wrong
    arrives buried under a second failure about a connection that went away.
    """
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
