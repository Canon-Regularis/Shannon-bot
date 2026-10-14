"""A delivery carries what its thread asked for. Issue #163.

Read off the row the sync already holds, so every line decided after the sync obeys the thread
without asking the database again - including for a delivery the sync found superseded, which is
still announced, because a tag line says what happened rather than what is.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType, Status
from shannon.domain.tracking import TrackedEvent, Tracking
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.announcements import Arrival
from shannon.services.sync.items import (
    ItemSyncService,
    SyncOutcome,
    build_item_handler,
    build_item_sync,
)
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import blocked_on_a_row, track

pytestmark = pytest.mark.integration

LATER = "2026-08-10T13:00:00Z"
LATER_STILL = "2026-08-10T14:00:00Z"


class Heard:
    """An announcer that only remembers what it was handed."""

    def __init__(self) -> None:
        self.arrivals: list[Arrival] = []

    async def say(self, arrival: Arrival) -> None:
        self.arrivals.append(arrival)


class TestTheSyncsAnswer:
    async def test_a_thread_nobody_chose_for_answers_the_defaults(
        self, registered: Repository, sync_service: ItemSyncService, pr_event, thread_id: int
    ) -> None:
        result = await sync_service.sync(pr_event("edited", updated_at=LATER))

        assert result.synced
        assert result.tracking == Tracking()

    async def test_a_sync_answers_with_what_the_thread_chose(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        pr_event,
        thread_id: int,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await track(db_sessionmaker, thread_id=thread_id, on=(TrackedEvent.LABELS,))

        result = await sync_service.sync(pr_event("edited", updated_at=LATER))

        assert result.synced
        assert result.tracking.wants(TrackedEvent.LABELS) is True

    async def test_a_superseded_delivery_still_carries_it(
        self,
        registered: Repository,
        sync_service: ItemSyncService,
        pr_event,
        thread_id: int,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """It is still announced, so it has to obey the thread as much as a current one does."""
        await track(db_sessionmaker, thread_id=thread_id, on=(TrackedEvent.LABELS,))
        await sync_service.sync(pr_event("edited", updated_at=LATER_STILL))

        result = await sync_service.sync(pr_event("edited", updated_at=LATER))

        assert result.outcome is SyncOutcome.STALE
        assert result.tracking.wants(TrackedEvent.LABELS) is True

    async def test_a_brand_new_item_another_sync_opened_first_carries_it_too(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        pr_event,
    ) -> None:
        """The other way a delivery is found superseded: after the insert rather than before.

        Both syncs of a brand-new item find no row, so the loser only learns it lost once the
        insert hands it the winner's row - and what the thread chose is on that row by then.
        """
        service = build_item_sync(db_sessionmaker, FakeThreadGateway(), PullRequestPolicy())
        older = pr_event("opened", updated_at="2026-08-10T18:00:00Z")

        async with db_sessionmaker() as holder:
            await holder.begin()
            item = await TrackedItemStore(holder).get_or_create(
                repository_id=registered.id,
                object_type=ObjectType.PR,
                github_object_id=payloads.PR_ID,
                github_object_number=7,
                github_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
                title="Renamed since",
                github_state="open",
                status=Status.NOT_REVIEWED,
                github_updated_at=datetime(2026, 8, 10, 18, 0, 5, tzinfo=UTC),
            )
            item.discord_thread_id = 4242
            item.discord_message_id = 99
            item.tracking = {TrackedEvent.COMMITS.value: True}
            await holder.flush()

            catching_up = asyncio.create_task(service.sync(older))
            await blocked_on_a_row(db_sessionmaker, catching_up)

            await holder.commit()
            result = await catching_up

        assert result.outcome is SyncOutcome.STALE
        assert result.tracking.wants(TrackedEvent.COMMITS) is True


class TestWhatAnAnnouncerIsHanded:
    async def test_the_threads_choice_reaches_whatever_says_something_about_a_delivery(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        heard = Heard()
        handle = build_item_handler(
            build_item_sync(db_sessionmaker, threads, PullRequestPolicy()),
            parse_pull_request_event,
            announce=heard,
        )
        await handle("opened", payloads.pull_request_event("opened"), 900_001)
        await track(
            db_sessionmaker,
            thread_id=heard.arrivals[0].thread_id,
            on=(TrackedEvent.LABELS,),
            off=(TrackedEvent.COMMENTS,),
        )

        await handle("edited", payloads.pull_request_event("edited", updated_at=LATER), 900_002)

        assert heard.arrivals[-1].tracking == Tracking(
            on=frozenset({TrackedEvent.LABELS}), off=frozenset({TrackedEvent.COMMENTS})
        )
