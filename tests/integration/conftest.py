from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.discord_bot.formatting import format_assignee_ping, format_reviewer_ping
from shannon.domain.enums import ActorRole, ObjectType
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.item_sync import ItemSyncService
from shannon.services.notifications import ActorNotifier
from shannon.services.policies import IssuePolicy, PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import map_channel, register_repository


@pytest.fixture
async def registered(db_session: AsyncSession) -> Repository:
    """A repository with both channel mappings, as /register then /set_channel leave things."""
    repository = await register_repository(db_session)
    await map_channel(db_session, repository, ObjectType.ISSUE, channel_id=98)
    return repository


@pytest.fixture
def threads() -> FakeThreadGateway:
    return FakeThreadGateway()


@pytest.fixture
def sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    """Pull request sync without the notifier, for tests that are not about pinging."""
    return ItemSyncService(db_sessionmaker, threads, PullRequestPolicy())


@pytest.fixture
def notifying_sync_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return ItemSyncService(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
        ),
    )


@pytest.fixture
def issue_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return ItemSyncService(db_sessionmaker, threads, IssuePolicy())


@pytest.fixture
def notifying_issue_service(
    db_sessionmaker: async_sessionmaker, threads: FakeThreadGateway
) -> ItemSyncService:
    return ItemSyncService(
        db_sessionmaker,
        threads,
        IssuePolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.ASSIGNEE, render=format_assignee_ping
        ),
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


@pytest.fixture
def issue_event():
    def build(action: str = "opened", **overrides: object):
        snapshot = parse_issue_event(action, payloads.issue_event(action, **overrides))
        assert snapshot is not None, f"payload for {action} did not parse"
        return snapshot

    return build
