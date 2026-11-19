"""`claim_thread` answers a win from its own UPDATE. Issue #268.

The write path swaps a thread for itself after every ordinary delivery, so a claim that wins is
the common case, and the row it just wrote is already the answer. Only a claim that found nothing
to write has to read the row. `test_what_the_stores_guard` pins what each answer is; these pin
what it costs.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.domain.enums import CardState, ObjectType
from tests.integration.test_query_behaviour import QueryLog

pytestmark = pytest.mark.integration

THREAD = 5551
MESSAGE = 6661


async def an_item(session: AsyncSession, repository: Repository) -> int:
    item = TrackedItem(
        repository_id=repository.id,
        github_object_id=900_001,
        github_object_type=ObjectType.TICKET,
        github_object_number=12,
        github_url="https://github.com/orgs/o/projects/1",
        title="seeded",
        github_state=CardState.OPEN.value,
        discord_thread_id=THREAD,
        discord_message_id=MESSAGE,
    )
    session.add(item)
    await session.commit()
    return item.id


async def statements_for_a_claim(
    engine: AsyncEngine, session: AsyncSession, item_id: int, *, replacing: int | None
) -> tuple[tuple[int | None, int | None], list[str]]:
    log = QueryLog(engine)
    try:
        answer = await ThreadPointerStore(session).claim_thread(
            item_id, thread_id=THREAD, message_id=7777, replacing=replacing
        )
    finally:
        log.close()
    sent = [s for s in log.statements if "tracked_items" in s]
    return answer, sent


async def test_a_claim_that_wins_sends_only_its_update(
    db_engine: AsyncEngine, db_session: AsyncSession, registered: Repository
) -> None:
    """The self-swap after an ordinary delivery: the same thread, a moved message id."""
    item_id = await an_item(db_session, registered)

    answer, sent = await statements_for_a_claim(db_engine, db_session, item_id, replacing=THREAD)

    assert answer == (THREAD, 7777)
    assert len(sent) == 1
    assert sent[0].startswith("UPDATE tracked_items")


async def test_a_claim_that_loses_reads_the_row_it_lost_to(
    db_engine: AsyncEngine, db_session: AsyncSession, registered: Repository
) -> None:
    item_id = await an_item(db_session, registered)

    answer, sent = await statements_for_a_claim(db_engine, db_session, item_id, replacing=None)

    assert answer == (THREAD, MESSAGE)
    assert [s.split()[0] for s in sent] == ["UPDATE", "SELECT"]
