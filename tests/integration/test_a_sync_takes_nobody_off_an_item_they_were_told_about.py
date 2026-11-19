"""Who `ItemAssignmentStore.replace` takes off an item, and who it leaves. Issue #268.

The guard against an older payload deleting a row somebody has been pinged from used to be
decided in Python from an unlocked read, and the delete then matched on the name alone. The
notifier stamps in its own transaction, so a stamp that landed between the two was deleted with
the row and the next event pinged the same person again. These pin the guard case by case, and the
race it now closes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ItemAssignment, Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.domain.enums import ActorRole, CardState, ObjectType
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

REQUESTED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
TOLD = datetime(2026, 8, 10, 12, 5, tzinfo=UTC)
SECOND = timedelta(seconds=1)


async def an_item(session: AsyncSession, repository: Repository) -> int:
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=900_001,
        github_object_type=ObjectType.TICKET,
        github_object_number=12,
        github_url="https://github.com/orgs/o/projects/1",
        title="seeded",
        github_state=CardState.OPEN.value,
    )
    session.add(item)
    await session.commit()
    return item.id


async def a_reviewer(
    session: AsyncSession,
    tracked_item_id: int,
    *,
    requested_at: datetime | None,
    notified_at: datetime | None,
) -> None:
    session.add(
        ItemAssignment(
            tracked_item_id=tracked_item_id,
            github_username="monalisa",
            github_user_id=200,
            role_type=ActorRole.REVIEWER,
            requested_at=requested_at,
            notified_at=notified_at,
        )
    )
    await session.commit()


async def reviewers(session: AsyncSession) -> list[tuple[str, datetime | None]]:
    session.expire_all()
    rows = await session.execute(
        select(ItemAssignment.github_username, ItemAssignment.notified_at).where(
            ItemAssignment.role_type == ActorRole.REVIEWER
        )
    )
    return [(name, told) for name, told in rows.all()]


@pytest.mark.parametrize(
    ("requested_at", "notified_at", "as_of", "kept"),
    [
        pytest.param(REQUESTED, None, REQUESTED, False, id="nobody-told-yet"),
        pytest.param(REQUESTED, TOLD, None, False, id="payload-without-a-time"),
        pytest.param(None, TOLD, REQUESTED, False, id="row-from-before-the-column"),
        pytest.param(REQUESTED, TOLD, REQUESTED + SECOND, False, id="payload-newer"),
        pytest.param(REQUESTED, TOLD, REQUESTED, True, id="payload-same-second"),
        pytest.param(REQUESTED, TOLD, REQUESTED - SECOND, True, id="payload-older"),
        pytest.param(
            REQUESTED, TOLD, REQUESTED.replace(tzinfo=None), True, id="naive-payload-read-as-utc"
        ),
        pytest.param(
            REQUESTED,
            TOLD,
            REQUESTED.astimezone(timezone(timedelta(hours=2))),
            True,
            id="same-instant-other-offset",
        ),
    ],
)
async def test_a_payload_without_the_reviewer_removes_them_unless_they_were_told_since(
    registered: Repository,
    db_session: AsyncSession,
    requested_at: datetime | None,
    notified_at: datetime | None,
    as_of: datetime | None,
    kept: bool,
) -> None:
    item_id = await an_item(db_session, registered)
    await a_reviewer(db_session, item_id, requested_at=requested_at, notified_at=notified_at)

    await ItemAssignmentStore(db_session).replace(
        tracked_item_id=item_id, role=ActorRole.REVIEWER, actors=[], as_of=as_of
    )
    await db_session.commit()

    assert [name for name, _ in await reviewers(db_session)] == (["monalisa"] if kept else [])


async def test_a_removal_does_not_delete_a_ping_claimed_while_it_ran(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The notifier claims in its own transaction, holding no item lock, after the sync commits.

    A second sync carrying the same second reads the row before the claim commits, sees nobody
    told, and decides the row may go. Postgres re-checks the delete's WHERE on the row once the
    claim lets go of it, so the guard has to be in that WHERE for the stamp to save the row.

    Sequenced rather than timed, the same way as the rename test in test_concurrent_sync.py.
    """
    item_id = await an_item(db_session, registered)
    await a_reviewer(db_session, item_id, requested_at=REQUESTED, notified_at=None)

    may_read = asyncio.Event()
    claim_done = asyncio.Event()

    async def claim() -> None:
        async with db_sessionmaker() as session, session.begin():
            claimed = await ItemAssignmentStore(session).claim_notifications(
                item_id, ActorRole.REVIEWER
            )
            assert claimed == {"monalisa": 200}
            may_read.set()
            await claim_done.wait()

    async def remove() -> None:
        await may_read.wait()
        async with db_sessionmaker() as session, session.begin():
            # Reads the row while the claim is still uncommitted, so it sees no stamp.
            removing = asyncio.create_task(
                ItemAssignmentStore(session).replace(
                    tracked_item_id=item_id, role=ActorRole.REVIEWER, actors=[], as_of=REQUESTED
                )
            )
            try:
                # Let it get as far as the delete, which parks on the row the claim is holding.
                await blocked_on_a_row(db_sessionmaker, removing)
            finally:
                # Set whatever happened above, or `claim` waits inside an open transaction
                # into the fixture's teardown.
                claim_done.set()
            await removing

    await asyncio.gather(claim(), remove())

    found = await reviewers(db_session)
    assert [name for name, _ in found] == ["monalisa"], "the stamped row was deleted"
    assert found[0][1] is not None, "the row survived without its stamp"
