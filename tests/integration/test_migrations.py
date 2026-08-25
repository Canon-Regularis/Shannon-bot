from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from shannon.db.base import Base
from shannon.domain.enums import DeliveryStatus

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
SCRATCH_DATABASE = "shannon_migration_check"

# These tests are deliberately synchronous. Alembic's env.py drives the migration with
# asyncio.run, which cannot be called from inside a running event loop.


@pytest.fixture
def migration_url(database_url: str) -> Iterator[str]:
    """A throwaway database with nothing in it, so migrations start from truly empty."""
    base = make_url(database_url)
    # str() on a SQLAlchemy URL masks the password as literal asterisks, which then get sent
    # to the server as the password.
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    scratch = base.set(database=SCRATCH_DATABASE).render_as_string(hide_password=False)

    asyncio.run(_recreate(admin, SCRATCH_DATABASE))
    try:
        yield scratch
    finally:
        asyncio.run(_drop(admin, SCRATCH_DATABASE))


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    # Both paths are made absolute because the working directory in CI is not guaranteed.
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migrations_apply_to_an_empty_database(migration_url: str) -> None:
    command.upgrade(alembic_config(migration_url), "head")

    tables = asyncio.run(_table_names(migration_url))
    assert tables == {
        "alembic_version",
        "channel_mappings",
        "item_assignments",
        "mirrored_notes",
        "repositories",
        "team_links",
        "tracked_items",
        "user_links",
        "webhook_events",
    }


def test_the_migration_and_the_models_agree(migration_url: str) -> None:
    """The check that stops the two drifting apart.

    An ORM change without a matching revision produces a diff here, which is the failure that
    would otherwise only show up as a confusing error in production.
    """
    command.upgrade(alembic_config(migration_url), "head")

    differences = asyncio.run(_diff(migration_url))

    assert differences == [], f"migration does not match the models: {differences}"


def test_the_live_index_covers_exactly_the_live_statuses(migration_url: str) -> None:
    """The one thing the schema diff above cannot see, and the models say it can.

    Alembic's PostgreSQL comparison ignores an index's WHERE clause, so widening, narrowing or
    deleting the predicate on `ix_webhook_events_live` all leave `compare_metadata` answering
    with nothing. That index is what keeps the lease off a full table scan, and its predicate is
    the whole reason it stays small however long deliveries are kept, so it is worth reading out
    of the database rather than trusting.

    Read from `pg_indexes` rather than from the model, so what is checked is what a deployment
    actually has: a sixth delivery status added to `live()` without a migration fails here.
    """
    command.upgrade(alembic_config(migration_url), "head")

    definition = asyncio.run(_index_definition(migration_url, "ix_webhook_events_live"))

    assert definition is not None, "the index the lease depends on is not there"
    named = set(re.findall(r"'([A-Z_]+)'::", definition))
    assert named == {status.value for status in DeliveryStatus.live()}, definition


def test_migrations_roll_back(migration_url: str) -> None:
    config = alembic_config(migration_url)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert asyncio.run(_table_names(migration_url)) == {"alembic_version"}


def test_migrations_are_reapplyable_after_a_rollback(migration_url: str) -> None:
    config = alembic_config(migration_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    command.upgrade(config, "head")

    assert asyncio.run(_diff(migration_url)) == []


def test_there_is_exactly_one_head() -> None:
    """Two heads mean two revisions claim the same parent, and `upgrade head` becomes ambiguous."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://unused/unused"))

    assert len(script.get_heads()) == 1


async def _index_definition(url: str, name: str) -> str | None:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
        )
    await engine.dispose()
    return found


async def _recreate(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()


async def _drop(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await engine.dispose()


async def _table_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await engine.dispose()
    return names


async def _diff(url: str) -> list:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as connection:
        differences = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    await engine.dispose()
    return differences
