"""A thread that turned `checks` off hears no CI result, and GitHub is not asked. Issue #163.

A result is three GitHub reads before it is a line - the pull request, its checks and who wrote
its commits - so the thread's answer is read first, off the row that says where the thread is.
Nothing is claimed for a result that is not said, so one asked for again later is said then.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote
from shannon.domain.models import Actor, CheckRun, CommitRef
from shannon.domain.tracking import TrackedEvent
from shannon.github import mapping
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import register_repository, track
from tests.support.stack import DeliveryClient, build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()


def a_github() -> FakeGitHubClient:
    """A GitHub holding the pull request, a green run on its commit, and who wrote it."""
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    snapshot = mapping.pull_request(payloads.pull_request(), repo)
    assert snapshot is not None
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): snapshot})
    github.check_runs[payloads.CHECKED_SHA] = [
        CheckRun(
            check_run_id=1,
            name="Tests",
            status="completed",
            conclusion="success",
            html_url="https://github.com/o/r/actions/runs/1/job/1",
        )
    ]
    github.pull_request_commits[(REPO_FULL, 7)] = [
        CommitRef(sha="a" * 40, message="Add the thing", author=Actor("defunkt"), merge=False)
    ]
    return github


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
) -> AsyncIterator[tuple[DeliveryClient, FakeGitHubClient]]:
    """A pull request whose thread has turned CI results off."""
    await register_repository(db_session, guild_id=1, channel_id=99)
    github = a_github()
    async with build_http_client(build_stack(db_engine, threads=threads, github=github)) as client:
        await deliver(client, "pull_request", payloads.pull_request_event("opened"), delivery="p0")
        await track(
            db_sessionmaker, thread_id=threads.created[0].thread_id, off=(TrackedEvent.CHECKS,)
        )
        yield client, github


async def a_suite(client: DeliveryClient, *, delivery: str) -> None:
    await deliver(client, "check_suite", payloads.check_suite_event(), delivery=delivery)


async def test_a_finished_suite_says_nothing_and_asks_github_nothing(
    tracked: tuple[DeliveryClient, FakeGitHubClient],
    threads: FakeThreadGateway,
    db_session: AsyncSession,
) -> None:
    client, github = tracked

    await a_suite(client, delivery="cs-1")

    assert await client.outcome_of("cs-1") == "ignored"
    assert threads.posts == []
    assert github.pull_request_calls == []
    assert github.check_run_calls == []
    assert github.commit_list_calls == []
    held = await db_session.scalar(select(func.count()).select_from(MirroredNote))
    assert held == 0, "a result that was never said was recorded as said"


async def test_turned_back_on_the_same_results_are_said_when_they_come_round_again(
    tracked: tuple[DeliveryClient, FakeGitHubClient],
    threads: FakeThreadGateway,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A re-run, say. Nothing was claimed for the first time round, so nothing stands in the way."""
    client, _ = tracked
    await a_suite(client, delivery="cs-1")
    await track(db_sessionmaker, thread_id=threads.created[0].thread_id, on=(TrackedEvent.CHECKS,))

    await a_suite(client, delivery="cs-2")

    assert await client.outcome_of("cs-2") == "processed"
    assert len(threads.posts) == 1
    assert "succeeded." in threads.posts[0][1]
