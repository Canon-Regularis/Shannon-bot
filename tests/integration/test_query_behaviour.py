from __future__ import annotations

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.webhook_events import WebhookEventStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import IssuePolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration


class QueryLog:
    """Counts the statements an engine actually sends."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.statements: list[str] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._record)
        self._engine = engine

    def _record(self, conn, cursor, statement, params, context, many) -> None:
        self.statements.append(" ".join(statement.split()))

    def touching(self, table: str) -> list[str]:
        return [s for s in self.statements if table in s and s.upper().startswith("SELECT")]

    def close(self) -> None:
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._record)


async def test_a_sync_never_loads_assignments_it_does_not_read(
    registered: Repository,
    db_engine: AsyncEngine,
    db_sessionmaker: async_sessionmaker,
    issue_event,
) -> None:
    """Assignments are read one role at a time through the store.

    The relationship was eager loading on every fetch of a tracked item, which is the hottest
    path there is, for data nothing looked at.
    """
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), IssuePolicy())
    await service.sync(issue_event("opened"))

    log = QueryLog(db_engine)
    try:
        await service.sync(issue_event("edited", title="Renamed"))
    finally:
        log.close()

    selects = log.touching("item_assignments")
    # One per role the issue policy stores: author and assignee. Anything more is the
    # relationship loading behind the store's back.
    assert len(selects) == 2, "\n".join(selects)


async def test_reading_the_relationship_is_an_error_rather_than_a_silent_query(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    issue_event,
) -> None:
    """If someone reaches for it later, they should find out immediately."""
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), IssuePolicy())
    result = await service.sync(issue_event("opened"))

    async with db_sessionmaker() as session:
        item = await TrackedItemStore(session).get_by_id(result.tracked_item_id)
        assert item is not None
        with pytest.raises(InvalidRequestError):
            _ = item.assignments


async def test_finding_an_item_by_number_uses_an_index(
    registered: Repository,
    db_session: AsyncSession,
    issue_event,
    db_sessionmaker: async_sessionmaker,
) -> None:
    """Comments and reviews take this path, so it has to stay flat as a repository grows.

    Explained against a populated table, and analysed first. On one row a sequential scan really
    is cheaper and PostgreSQL is right to choose it, so a plan taken there says nothing about
    what happens at size and flips according to whether autovacuum has got round to the table
    yet. The number below is far past the point where the choice stops being a close call.
    """
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), IssuePolicy())
    await service.sync(issue_event("opened"))
    await _fill_with_items(db_session, registered.id, count=2000)

    plan = await db_session.execute(
        text(
            "EXPLAIN SELECT * FROM tracked_items "
            f"WHERE repository_id = {registered.id} AND github_object_number = 12"
        )
    )
    rendered = "\n".join(row[0] for row in plan)

    assert "Seq Scan" not in rendered, rendered
    assert "github_object_number" in rendered, rendered


async def test_the_number_lookup_still_finds_the_right_row(
    registered: Repository,
    db_session: AsyncSession,
    issue_event,
    pr_event,
    db_sessionmaker: async_sessionmaker,
) -> None:
    issues = build_item_sync(db_sessionmaker, FakeThreadGateway(), IssuePolicy())
    await issues.sync(issue_event("opened"))

    found = await TrackedItemStore(db_session).get_by_number(
        repository_id=registered.id, number=12, object_type=ObjectType.ISSUE
    )

    assert found is not None
    assert found.github_object_type is ObjectType.ISSUE
    assert isinstance(found, TrackedItem)


class TestHandingBackNothing:
    """Both stores are asked to release an empty set on paths that reach them normally.

    A worker cancelled on the last delivery of its batch hands back the empty remainder, and a
    notifier that claimed nobody releases nobody. Neither should reach the database: `IN ()` is
    not valid SQL, and SQLAlchemy renders it as a always-false expression that costs a round
    trip to learn nothing.
    """

    async def test_releasing_no_deliveries_sends_no_statement(
        self, db_engine: AsyncEngine, db_session: AsyncSession
    ) -> None:
        log = QueryLog(db_engine)
        try:
            await WebhookEventStore(db_session).release([])
        finally:
            log.close()

        assert log.statements == []

    async def test_releasing_no_pings_sends_no_statement(
        self, db_engine: AsyncEngine, db_session: AsyncSession
    ) -> None:
        log = QueryLog(db_engine)
        try:
            await ItemAssignmentStore(db_session).release_notifications(1, ActorRole.REVIEWER, [])
        finally:
            log.close()

        assert log.statements == []


async def _fill_with_items(session: AsyncSession, repository_id: int, *, count: int) -> None:
    """Enough rows that an index is unambiguously the cheaper way in, and statistics to prove it.

    Inserted in one statement rather than through the service: what is under test is the shape of
    the query, and two thousand round trips would make this the slowest test in the suite for no
    extra confidence.
    """
    await session.execute(
        text(
            "INSERT INTO tracked_items ("
            "  repository_id, github_object_id, github_object_type, github_object_number,"
            "  github_url, title, github_state, status, priority"
            ") SELECT :repository_id, 900000 + n, 'ISSUE', 900000 + n,"
            "  'https://example.invalid/' || n, 'Filler ' || n, 'open', 'NOT_REVIEWED', 'UNSET'"
            " FROM generate_series(1, :count) AS n"
        ),
        {"repository_id": repository_id, "count": count},
    )
    await session.commit()
    await session.execute(text("ANALYZE tracked_items"))
