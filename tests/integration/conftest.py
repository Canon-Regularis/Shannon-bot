from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.notifications import ReviewerNotifier
from shannon.services.pr_sync import PullRequestSyncService
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import register_repository


@pytest.fixture
async def registered(db_session: AsyncSession) -> Repository:
    return await register_repository(db_session)


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest.fixture
def sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> PullRequestSyncService:
    """Sync without the notifier, for tests that are not about pinging."""
    return PullRequestSyncService(db_sessionmaker, threads)


@pytest.fixture
def notifying_sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> PullRequestSyncService:
    return PullRequestSyncService(
        db_sessionmaker, threads, ReviewerNotifier(db_sessionmaker, threads)
    )


@pytest.fixture
def pr_event():
    def build(action: str = "opened", **overrides: object):
        snapshot = parse_pull_request_event(
            action, payloads.pull_request_event(action, **overrides)
        )
        assert snapshot is not None, f"payload for {action} did not parse"
        return snapshot

    return build
