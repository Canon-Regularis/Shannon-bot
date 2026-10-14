"""A thread that turned `people` off is rung for nobody, and owes nobody a ring later. Issue #163.

The ping is spent rather than kept. Keeping it would make turning `people` back on ring everybody
asked or assigned in the meantime, all at once, for requests that may be long settled - so the
switch is about what happens from now on, the same as every other kind of line.

The block is not a line and is not chosen: it still names everybody it names.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _notifying
from shannon.db.models import ItemAssignment, Repository
from shannon.db.stores.team_links import TeamLinkStore
from shannon.discord_bot.formatting import format_reviewer_ping, format_team_ping
from shannon.domain.enums import ActorRole
from shannon.domain.tracking import TrackedEvent
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import track

pytestmark = pytest.mark.integration

# Earlier than every payload below, so opening the thread is never the newest thing that happened
# to the item.
BEFORE = "2026-08-10T11:00:00Z"
BEFORE_THE_ISSUE = "2026-08-11T08:00:00Z"

ROLE = 777000


async def told(db_session: AsyncSession, role: ActorRole) -> dict[str, bool]:
    """Who in one role has had their ping spent, whether or not anything was said."""
    rows = await db_session.execute(
        select(ItemAssignment.github_username, ItemAssignment.notified_at).where(
            ItemAssignment.role_type == role
        )
    )
    return {login: at is not None for login, at in rows}


async def opened_with_nobody_asked(service: ItemSyncService, pr_event) -> int:
    """The thread already open, with no review asked of anybody yet.

    The block that opens a thread is a real message that reaches everybody it names, so a test
    about the line has to start with nobody named, or it passes on the block instead.
    """
    result = await service.sync(pr_event("opened", requested_reviewers=[], updated_at=BEFORE))
    assert result.thread_id is not None
    return result.thread_id


class TestAReviewer:
    async def test_asked_while_people_is_off_is_not_rung_and_is_owed_nothing(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        thread_id = await opened_with_nobody_asked(notifying_sync_service, pr_event)
        await track(db_sessionmaker, thread_id=thread_id, off=(TrackedEvent.PEOPLE,))

        result = await notifying_sync_service.sync(pr_event("review_requested"))

        assert result.notified == ()
        assert threads.posts == []
        assert await told(db_session, ActorRole.REVIEWER) == {"monalisa": True}
        assert "monalisa" in threads.metadata_of(thread_id), "the block is not a line"

    async def test_turned_back_on_rings_only_whoever_is_asked_next(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        pr_event,
    ) -> None:
        thread_id = await opened_with_nobody_asked(notifying_sync_service, pr_event)
        await track(db_sessionmaker, thread_id=thread_id, off=(TrackedEvent.PEOPLE,))
        await notifying_sync_service.sync(pr_event("review_requested"))
        await track(db_sessionmaker, thread_id=thread_id, on=(TrackedEvent.PEOPLE,))

        caught_up = await notifying_sync_service.sync(
            pr_event("edited", title="Renamed", updated_at="2026-08-10T12:30:00Z")
        )
        asked = await notifying_sync_service.sync(
            pr_event(
                "review_requested",
                requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
                updated_at="2026-08-10T13:00:00Z",
            )
        )

        assert caught_up.notified == (), "it rang somebody asked while it was off"
        assert asked.notified == ("hubot",)
        assert len(threads.posts) == 1
        assert "hubot" in threads.posts[0][1]
        assert "monalisa" not in threads.posts[0][1]

    async def test_a_thread_nobody_chose_for_still_rings(
        self,
        registered: Repository,
        notifying_sync_service: ItemSyncService,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        await opened_with_nobody_asked(notifying_sync_service, pr_event)

        result = await notifying_sync_service.sync(pr_event("review_requested"))

        assert result.notified == ("monalisa",)
        assert len(threads.posts) == 1


class TestATeam:
    async def test_asked_while_people_is_off_is_not_rung_either(
        self,
        registered: Repository,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        pr_event,
    ) -> None:
        """The one notifier the block does not stand in for, so it needs the thread's word too."""
        service = build_item_sync(
            db_sessionmaker,
            threads,
            PullRequestPolicy(),
            _notifying(
                ActorNotifier(
                    db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
                ),
                ActorNotifier(
                    db_sessionmaker,
                    threads,
                    role=ActorRole.REVIEWER_TEAM,
                    render=format_team_ping,
                    mentions=TeamLinkStore,
                    the_block_pings_them=False,
                ),
            ),
        )
        await TeamLinkStore(db_session).link(
            guild_id=1, github_team="backend", discord_role_id=ROLE
        )
        await db_session.commit()
        thread_id = await opened_with_nobody_asked(service, pr_event)
        await track(db_sessionmaker, thread_id=thread_id, off=(TrackedEvent.PEOPLE,))

        result = await service.sync(
            pr_event(
                "review_requested", requested_reviewers=[], requested_teams=[{"slug": "backend"}]
            )
        )

        assert result.notified == ()
        assert threads.posts == [], "the team's role was rung"
        assert await told(db_session, ActorRole.REVIEWER_TEAM) == {"backend": True}


class TestAnIssueAssignee:
    async def test_assigned_while_people_is_off_is_not_rung(
        self,
        registered: Repository,
        notifying_issue_service: ItemSyncService,
        threads: FakeThreadGateway,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        issue_event,
    ) -> None:
        opened = await notifying_issue_service.sync(
            issue_event("opened", assignees=[], updated_at=BEFORE_THE_ISSUE)
        )
        assert opened.thread_id is not None
        await track(db_sessionmaker, thread_id=opened.thread_id, off=(TrackedEvent.PEOPLE,))

        result = await notifying_issue_service.sync(issue_event("assigned"))

        assert result.notified == ()
        assert threads.posts == []
        assert await told(db_session, ActorRole.ASSIGNEE) == {"hubot": True}
