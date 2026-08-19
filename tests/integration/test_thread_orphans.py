"""A thread that reached Discord must reach the row that owns it, even on the way out.

The worker puts a deadline on each delivery and shutdown cancels it outright when the grace runs
out, and either can land between Discord creating a thread and the row being told about it.
Nothing reconciles orphans: `discord_thread_id` is only ever read off a row found some other way,
so a thread id that never got written is unreachable by anything. The retry finds no thread,
opens a second one, and the first sits in the channel receiving no comment, review or ping for
the rest of its life.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import Repository, TrackedItem
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.threads import ThreadHandle
from shannon.github.webhooks.pull_request import parse_pull_request_event
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import build_stack

pytestmark = pytest.mark.integration


class SlowToAnswer(FakeThreadGateway):
    """Discord makes the thread and takes its time saying so.

    That is the real shape of it: the gateway creates the thread and then sends the first
    message, and the sending half is what discord.py sleeps through when it is rate limited.
    """

    def __init__(self, delay: float = 0.3) -> None:
        super().__init__()
        self.delay = delay
        # Set once Discord has the thread and before the answer comes back, so a test cancels at
        # the point it means to rather than wherever a sleep happens to land. The first try used
        # a sleep and cancelled during the database work instead, which proved nothing.
        self.made_one = asyncio.Event()

    async def create(self, **kwargs) -> ThreadHandle:
        handle = await super().create(**kwargs)
        self.made_one.set()
        await asyncio.sleep(self.delay)
        return handle


def an_opened_pull_request():
    snapshot = parse_pull_request_event("opened", payloads.pull_request_event("opened"))
    assert snapshot is not None
    return snapshot


async def test_a_cancellation_mid_creation_still_attaches_the_thread(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    threads = SlowToAnswer()
    container = build_stack(db_engine, threads=threads)

    syncing = asyncio.create_task(container.pr_sync.sync(an_opened_pull_request()))
    await asyncio.wait_for(threads.made_one.wait(), timeout=5)
    syncing.cancel()
    await asyncio.wait({syncing})

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))

    assert threads.created, "the test never reached the point it is about"
    opened = [thread.thread_id for thread in threads.created]
    assert item.discord_thread_id in opened, (
        "a thread was opened in Discord that no row points at, so the retry opens another"
    )


async def test_the_cancellation_is_still_passed_on(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Waiting for the claim must not turn a cancelled delivery into a successful one.

    The worker hands the rest of its batch back when it is cancelled, and the delivery itself
    has to stay unfinished so it is tried again.
    """
    threads = SlowToAnswer()
    container = build_stack(db_engine, threads=threads)

    syncing = asyncio.create_task(container.pr_sync.sync(an_opened_pull_request()))
    await asyncio.wait_for(threads.made_one.wait(), timeout=5)
    syncing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await syncing


class FailsSlowly(FakeThreadGateway):
    """Discord takes its time and then refuses, which is a rate limit followed by a 500."""

    def __init__(self, delay: float = 0.3) -> None:
        super().__init__()
        self.delay = delay
        self.made_one = asyncio.Event()

    async def create(self, **kwargs) -> ThreadHandle:
        self.made_one.set()
        await asyncio.sleep(self.delay)
        raise DiscordGatewayError("Discord refused to create a thread")


async def test_a_failure_during_shutdown_is_reported_in_our_own_words(
    registered: Repository,
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing else is listening at this point, so the log is all anybody gets.

    A shielded future whose caller has been cancelled has its exception reported by asyncio
    instead, which says less and does not name the item.
    """
    threads = FailsSlowly()
    container = build_stack(db_engine, threads=threads)

    syncing = asyncio.create_task(container.pr_sync.sync(an_opened_pull_request()))
    await asyncio.wait_for(threads.made_one.wait(), timeout=5)
    with caplog.at_level("WARNING", logger="shannon.services.sync.threads"):
        syncing.cancel()
        await asyncio.wait({syncing})

    assert "failed as it was shutting down" in caplog.text
    assert "Discord refused to create a thread" in caplog.text


async def test_a_gateway_that_never_answers_does_not_hold_the_process_open(
    registered: Repository,
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is bounded, and what it gives up on is worth saying out loud.

    Ten seconds in production, cut here so the test does not take ten. A thread may or may not
    exist in the channel at this point, and nothing will ever point at it, so the log is the
    only trace of it there will be.
    """
    monkeypatch.setattr("shannon.services.sync.threads.CLAIM_GRACE_SECONDS", 0.05)
    threads = SlowToAnswer(delay=1.0)
    container = build_stack(db_engine, threads=threads)

    syncing = asyncio.create_task(container.pr_sync.sync(an_opened_pull_request()))
    await asyncio.wait_for(threads.made_one.wait(), timeout=5)
    with caplog.at_level("ERROR", logger="shannon.services.sync.threads"):
        syncing.cancel()
        await asyncio.wait({syncing})

    assert "gave up waiting for a thread to be attached" in caplog.text
    # The claim is still going. Letting it land keeps the loop from closing under it, which is
    # what production does not do and what makes the log line above true.
    await asyncio.sleep(1.2)


async def test_nothing_changes_when_nobody_cancels_anything(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    threads = SlowToAnswer(delay=0.0)
    container = build_stack(db_engine, threads=threads)

    result = await container.pr_sync.sync(an_opened_pull_request())

    db_session.expunge_all()
    item = await db_session.scalar(select(TrackedItem))
    assert (result.created, item.discord_thread_id) == (True, result.thread_id)


async def test_a_claim_that_cannot_be_written_takes_the_thread_back(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Which half failed is what makes this recoverable.

    Discord answered or there would be no thread to worry about; it is the database that did
    not. So the call that undoes the thread is the one still working, and using it is what stops
    the retry opening a second thread beside a first that nothing points at.
    """
    threads = SlowToAnswer(delay=0.0)
    container = build_stack(db_engine, threads=threads)

    binding = container.pr_sync._binding

    async def the_database_is_gone(*args, **kwargs):
        raise OSError("connection refused")

    binding._swap = the_database_is_gone

    with pytest.raises(OSError):
        await container.pr_sync.sync(an_opened_pull_request())

    opened = [thread.thread_id for thread in threads.created]
    assert opened, "the test never reached the point it is about"
    assert threads.deleted == opened, (
        "a thread was left in Discord that no row points at, so the retry opens another"
    )
