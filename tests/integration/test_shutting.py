"""Putting a thread back the way the row says it should be, after something wrote to it.

Its own file because it is the piece that makes archiving work at all, and it is the piece that
is easy to look at and think is redundant. Discord refuses every edit to an archived thread, so
every write reopens one first; without this the closing header reopens the thread it is
announcing the close of, and a comment on an issue somebody shut last week pulls it back into the
channel for good.

Driven directly rather than through a handler. What reaches it end to end is covered by
`test_state_lines` and `test_issue_state_changes`; what is here is the three answers it can give
and the refusal it has to swallow.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.services.sync.items import ItemSyncService
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

CLOSED = {"state": "closed", "closed_at": "2026-08-11T12:00:00Z"}


async def test_a_thread_the_row_says_is_shut_is_shut_again(
    registered: Repository,
    issue_service: ItemSyncService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    issue_event,
) -> None:
    result = await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    # What a post leaves behind, which is the state this exists to correct.
    threads.threads[result.thread_id].archived = False
    asked = len(threads.shut_calls)

    await KeepsThreadsShut(db_sessionmaker, threads).again(
        tracked_item_id=result.tracked_item_id, thread_id=result.thread_id
    )

    assert threads.shut_calls[asked:] == [(result.thread_id, True)]
    assert threads.threads[result.thread_id].archived is True


async def test_a_thread_on_an_open_item_is_left_alone(
    registered: Repository,
    issue_service: ItemSyncService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    issue_event,
) -> None:
    """The reason the row is asked rather than the thread. Discord archives a quiet thread by
    itself after a week, and one on an item still open wants leaving where it is: reading the
    thread would shut it for good the first time anybody commented on a stale pull request.
    """
    result = await issue_service.sync(issue_event("opened"))
    threads.threads[result.thread_id].archived = True
    asked = len(threads.shut_calls)

    await KeepsThreadsShut(db_sessionmaker, threads).again(
        tracked_item_id=result.tracked_item_id, thread_id=result.thread_id
    )

    assert threads.shut_calls[asked:] == [], "it shut a thread on an item nobody has finished"


async def test_an_item_that_is_no_longer_there_costs_no_call(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
) -> None:
    """Checked rather than trusted, the way the rest of this path checks it: the cost of being
    wrong is an attribute read on None inside a Discord call."""
    await KeepsThreadsShut(db_sessionmaker, threads).again(tracked_item_id=987_654, thread_id=1)

    assert threads.shut_calls == []


async def test_a_refusal_is_swallowed_and_said(
    registered: Repository,
    issue_service: ItemSyncService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    issue_event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The post has already landed and its claim is spent, so raising here would hand back a
    claim for a line that was said and the line would be lost on the retry.

    What it leaves behind is a thread locked but not archived, which is what this project shipped
    for a year, and Discord's own archive window shuts it within the week regardless.
    """
    result = await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    threads.threads[result.thread_id].archived = False
    threads.refuses_every_shut = True

    with caplog.at_level(logging.WARNING):
        await KeepsThreadsShut(db_sessionmaker, threads).again(
            tracked_item_id=result.tracked_item_id, thread_id=result.thread_id
        )

    assert "could not shut the thread" in caplog.text
    assert threads.threads[result.thread_id].archived is False


async def test_a_thread_that_is_gone_is_the_same_answer(
    registered: Repository,
    issue_service: ItemSyncService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    issue_event,
) -> None:
    """One arm for every refusal, deliberately. A deleted thread, a permission taken away and
    Discord having a bad minute all mean the same thing here: the line was said, the thread is
    not shut, and the next delivery for this item finds the row still asking.
    """
    result = await issue_service.sync(issue_event("opened"))
    await issue_service.sync(issue_event("closed", **CLOSED))
    threads.threads.pop(result.thread_id)

    await KeepsThreadsShut(db_sessionmaker, threads).again(
        tracked_item_id=result.tracked_item_id, thread_id=result.thread_id
    )
