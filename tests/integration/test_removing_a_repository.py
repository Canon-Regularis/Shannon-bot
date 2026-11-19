"""Counting what a repository holds, and removing it, through its store.

Issue #204, done under #268. /unregister used to write both statements by hand in the service; they
are `RepositoryStore.count_items` and `RepositoryStore.remove` now, and these pin what the service
relies on: the count is of this repository's items only, the delete takes everything keyed by the
repository with it through the foreign keys, and neither commits.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from shannon.db.models import ChannelMapping, Repository, TrackedItem
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType

pytestmark = pytest.mark.integration


async def a_repository(session: AsyncSession, number: int) -> Repository:
    return await RepositoryStore(session).add(
        github_repo_id=500 + number,
        repo_name=f"acme/widget-{number}",
        repo_url=f"https://github.com/acme/widget-{number}",
        discord_guild_id=number,
    )


async def items_under(session: AsyncSession, repository: Repository, count: int) -> None:
    for number in range(count):
        session.add(
            TrackedItem(
                repository_id=repository.id,
                github_object_type=ObjectType.PR,
                github_object_id=100 + number,
                github_object_number=number,
                title=f"Item {number}",
                github_url=f"{repository.repo_url}/pull/{number}",
                github_state="open",
            )
        )
    await session.flush()


async def rows_where(session: AsyncSession, criterion: ColumnElement[bool]) -> int:
    """How many rows match, read straight from the table rather than through a store."""
    counted = await session.scalar(select(func.count()).where(criterion))
    return counted or 0


class TestCountingItems:
    async def test_it_counts_the_items_under_the_repository(self, db_session: AsyncSession) -> None:
        repository = await a_repository(db_session, 1)
        await items_under(db_session, repository, 3)

        assert await RepositoryStore(db_session).count_items(repository.id) == 3

    async def test_another_repositorys_items_are_not_counted(
        self, db_session: AsyncSession
    ) -> None:
        repository = await a_repository(db_session, 1)
        other = await a_repository(db_session, 2)
        await items_under(db_session, other, 2)

        assert await RepositoryStore(db_session).count_items(repository.id) == 0

    async def test_an_unknown_repository_holds_nothing(self, db_session: AsyncSession) -> None:
        assert await RepositoryStore(db_session).count_items(999_999) == 0


class TestRemoving:
    async def test_it_takes_the_mappings_and_the_items_with_it(
        self, db_session: AsyncSession
    ) -> None:
        repository = await a_repository(db_session, 1)
        await ChannelMappingStore(db_session).set(
            repository_id=repository.id, object_type=ObjectType.PR, discord_channel_id=42
        )
        await items_under(db_session, repository, 2)
        repository_id = repository.id

        await RepositoryStore(db_session).remove(repository_id)

        assert await rows_where(db_session, Repository.id == repository_id) == 0
        assert await rows_where(db_session, ChannelMapping.repository_id == repository_id) == 0
        assert await rows_where(db_session, TrackedItem.repository_id == repository_id) == 0

    async def test_another_repository_is_left_alone(self, db_session: AsyncSession) -> None:
        repository = await a_repository(db_session, 1)
        other = await a_repository(db_session, 2)
        await items_under(db_session, other, 1)
        other_id = other.id

        await RepositoryStore(db_session).remove(repository.id)

        assert await rows_where(db_session, Repository.id == other_id) == 1
        assert await rows_where(db_session, TrackedItem.repository_id == other_id) == 1

    async def test_an_unknown_id_removes_nothing(self, db_session: AsyncSession) -> None:
        repository = await a_repository(db_session, 1)
        repository_id = repository.id

        await RepositoryStore(db_session).remove(999_999)

        assert await rows_where(db_session, Repository.id == repository_id) == 1

    async def test_it_does_not_commit(self, db_session: AsyncSession) -> None:
        """The service owns the transaction, so a rollback after it still has the repository."""
        repository = await a_repository(db_session, 1)
        await db_session.commit()
        repository_id = repository.id

        await RepositoryStore(db_session).remove(repository_id)
        await db_session.rollback()

        assert await rows_where(db_session, Repository.id == repository_id) == 1
