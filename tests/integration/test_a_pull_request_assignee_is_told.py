"""Somebody assigned to a pull request finds out, and the backlog that predates it does not.

Issue #105. Assignees on a pull request have been stored since the policy was written and nothing
has ever told them: the block names them, and every block after the first is an edit, which Discord
does not notify. Giving the pull request sync the assignee notifier fixes that going forward and
would, on its own, ping everybody already assigned to every open pull request, because none of
those rows was ever claimed. Migration 0021 is the half that stops it.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

HUBOT = 606


def _migration_sql():
    """The statement migration 0021 runs, read off the migration itself.

    Loaded by path because the module name starts with a digit and cannot be imported. Worth the
    awkwardness: a copy of this SQL in the test would be a copy that can drift, and what it has to
    get right is exactly which rows it does not touch.
    """
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0021_stop_a_backlog_of_assignees_being_pinged.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0021", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.STAMP


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    async with registered_stack(db_engine, db_session, threads) as http_client:
        # Opened with nobody on it, so the block has nobody to ping and the claim is still
        # there to be taken. Opened WITH an assignee, the block is a real message that reaches
        # them, spends the claim, and a later line would be the second time they were told.
        await deliver(
            http_client,
            "pull_request",
            payloads.pull_request_event("opened", assignees=[]),
            delivery="p0",
        )
        yield http_client


class TestTheMigration:
    """What it stamps and, more importantly, what it leaves alone."""

    async def _rows(self, db_session: AsyncSession) -> dict[tuple[ObjectType, str], ItemAssignment]:
        repository = await db_session.scalar(select(Repository))
        assert repository is not None
        made: dict[tuple[ObjectType, str], ItemAssignment] = {}
        for kind, number, object_id in (
            (ObjectType.PR, 501, 9001),
            (ObjectType.ISSUE, 502, 9002),
        ):
            item = TrackedItem(
                repository_id=repository.id,
                github_object_id=object_id,
                github_object_type=kind,
                github_object_number=number,
                github_url="",
                title="seeded",
            )
            db_session.add(item)
            await db_session.flush()
            for login, stamped in (("fresh", None), ("told", datetime(2026, 1, 1, tzinfo=UTC))):
                row = ItemAssignment(
                    tracked_item_id=item.id,
                    github_username=f"{login}-{kind.value}".lower(),
                    role_type=ActorRole.ASSIGNEE,
                    notified_at=stamped,
                )
                db_session.add(row)
                made[(kind, login)] = row
        await db_session.commit()
        return made

    async def test_a_pull_request_row_nobody_told_is_stamped(
        self, tracked: DeliveryClient, db_session: AsyncSession
    ) -> None:
        rows = await self._rows(db_session)

        await db_session.execute(_migration_sql())
        await db_session.commit()

        await db_session.refresh(rows[(ObjectType.PR, "fresh")])
        assert rows[(ObjectType.PR, "fresh")].notified_at is not None

    async def test_an_issue_row_is_left_alone(
        self, tracked: DeliveryClient, db_session: AsyncSession
    ) -> None:
        """A null on an issue is a ping genuinely still owed. The issue sync has carried this
        notifier all along and clears the stamp whenever a post fails, so stamping here would
        silence somebody who was about to be told."""
        rows = await self._rows(db_session)

        await db_session.execute(_migration_sql())
        await db_session.commit()

        await db_session.refresh(rows[(ObjectType.ISSUE, "fresh")])
        assert rows[(ObjectType.ISSUE, "fresh")].notified_at is None

    async def test_a_row_already_stamped_keeps_its_own_time(
        self, tracked: DeliveryClient, db_session: AsyncSession
    ) -> None:
        rows = await self._rows(db_session)
        before = rows[(ObjectType.PR, "told")].notified_at

        await db_session.execute(_migration_sql())
        await db_session.commit()

        await db_session.refresh(rows[(ObjectType.PR, "told")])
        assert rows[(ObjectType.PR, "told")].notified_at == before


class TestBeingTold:
    async def test_somebody_assigned_after_the_thread_opened_is_pinged(
        self, tracked: DeliveryClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The gap this closes. The opening block pings whoever it names, but every block after it
        is an edit, so until now a later assignee was named silently and told nothing."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=HUBOT
        )
        await db_session.commit()

        await deliver(
            tracked,
            "pull_request",
            payloads.pull_request_event("assigned", assignees=[payloads.user("hubot", 100)]),
            delivery="p1",
        )

        assert any(f"<@{HUBOT}>" in body for _, body in threads.posts), (
            "a pull request assignee was named in the block and told nowhere"
        )

    async def test_the_opening_block_still_speaks_for_itself(
        self, db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The block that opens a thread is a real message and so notifies everybody it names. A
        line beside it would reach the same person twice, which is issue #81."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=HUBOT
        )
        await db_session.commit()

        async with registered_stack(db_engine, db_session, threads) as client:
            await deliver(
                client,
                "pull_request",
                payloads.pull_request_event("opened", assignees=[payloads.user("hubot", 100)]),
                delivery="p0",
            )

        assert threads.posts == []
