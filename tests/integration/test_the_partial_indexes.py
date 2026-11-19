"""One open conversation per item, as `uq_logged_conversations_open_item` keeps it. Issue #268.

The index is partial (`WHERE stopped_at IS NULL`), so the rule is one open conversation per item,
not one ever. `ConversationStore.start` names that predicate as its ON CONFLICT target, so the
store works only while the deployed predicate matches it. Alembic's schema diff cannot see an
index's WHERE clause, and the rest of this tier builds its schema from the models, so the rule is
checked here twice: on the models' schema, and on the schema the migrations deploy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import func, make_url, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from shannon.db.models import LoggedConversation, Repository, TrackedItem
from shannon.db.stores.conversations import ConversationStore
from shannon.domain.enums import ObjectType
from tests.integration.test_migrations import alembic_config
from tests.support.db import register_repository

pytestmark = pytest.mark.integration

THREAD = 5551
STARTED = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
STOPPED = STARTED + timedelta(hours=1)

# Its own name rather than test_migrations' scratch database, so neither drops the other's.
SCRATCH_DATABASE = "shannon_partial_index_check"


async def an_item(
    session: AsyncSession, repository: Repository, *, object_id: int = 900_001
) -> int:
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=object_id,
        github_object_type=ObjectType.PR,
        github_object_number=12,
        github_url="https://github.com/o/r/pull/12",
        title="seeded",
        discord_thread_id=THREAD,
    )
    session.add(item)
    await session.commit()
    return item.id


def a_conversation(
    tracked_item_id: int, *, stopped_at: datetime | None = None
) -> LoggedConversation:
    return LoggedConversation(
        tracked_item_id=tracked_item_id,
        discord_thread_id=THREAD,
        started_by_discord_user_id=1,
        started_at=STARTED,
        stopped_at=stopped_at,
    )


async def conversations(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(LoggedConversation)) or 0


class TestTheIndexItself:
    """Rows written straight in, so nothing but the index stands in the way."""

    async def test_a_second_open_conversation_for_the_same_item_is_refused(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        db_session.add(a_conversation(item_id))
        await db_session.commit()

        db_session.add(a_conversation(item_id))
        with pytest.raises(IntegrityError, match="uq_logged_conversations_open_item"):
            await db_session.commit()

    async def test_a_new_one_is_allowed_once_the_first_has_stopped(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        item_id = await an_item(db_session, registered)
        db_session.add(a_conversation(item_id, stopped_at=STOPPED))
        await db_session.commit()

        db_session.add(a_conversation(item_id))
        await db_session.commit()

        assert await conversations(db_session) == 2

    async def test_another_item_may_have_one_open_at_the_same_time(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        first = await an_item(db_session, registered)
        second = await an_item(db_session, registered, object_id=900_002)
        db_session.add(a_conversation(first))
        db_session.add(a_conversation(second))
        await db_session.commit()

        assert await conversations(db_session) == 2


async def start_stop_start(session: AsyncSession, repository: Repository) -> list[int | None]:
    """What `start` answers while one is open, and again once it has stopped."""
    item_id = await an_item(session, repository)
    store = ConversationStore(session)

    async def start() -> int | None:
        return await store.start(
            tracked_item_id=item_id, discord_thread_id=THREAD, started_by=1, now=STARTED
        )

    answers = [await start(), await start()]
    await store.stop(tracked_item_id=item_id, stopped_by=1, now=STOPPED)
    answers.append(await start())
    await session.commit()
    return answers


class TestStartingThroughTheStore:
    """The ON CONFLICT target has to match the index, or PostgreSQL refuses the insert outright."""

    async def test_a_second_start_while_one_is_open_is_turned_away(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        first, second, _ = await start_stop_start(db_session, registered)

        assert first is not None
        assert second is None

    async def test_a_start_after_a_stop_opens_a_new_one(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        first, _, again = await start_stop_start(db_session, registered)

        assert again is not None
        assert again != first
        assert await conversations(db_session) == 2


@pytest.fixture
def migrated_url(database_url: str) -> Iterator[str]:
    """An empty database brought to head by the migrations, not built from the models."""
    base = make_url(database_url)
    # Rendered by hand: str() on a URL masks the password, which is then sent as the password.
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    scratch = base.set(database=SCRATCH_DATABASE).render_as_string(hide_password=False)

    asyncio.run(_recreate(admin))
    try:
        command.upgrade(alembic_config(scratch), "head")
        yield scratch
    finally:
        asyncio.run(_drop(admin))


class TestTheDeployedSchema:
    # Synchronous, because Alembic's env.py drives the upgrade with asyncio.run, which cannot be
    # called from inside a running event loop.
    def test_the_migrated_index_turns_a_second_away_and_lets_a_new_one_in(
        self, migrated_url: str
    ) -> None:
        """A migration that dropped the index, or the predicate, passes the schema diff.

        Without the index the ON CONFLICT clause raises. Without the predicate the start after
        the stop is turned away as well.
        """
        first, second, again = asyncio.run(_against(migrated_url))

        assert first is not None
        assert second is None
        assert again is not None


async def _against(url: str) -> list[int | None]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await start_stop_start(session, await register_repository(session))
    finally:
        await engine.dispose()


async def _recreate(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        await connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))
    await engine.dispose()


async def _drop(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
    await engine.dispose()
