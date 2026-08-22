from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ChannelMapping, ItemAssignment, Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ActorRole, ObjectType, Status
from shannon.domain.models import Actor
from shannon.domain.time import as_utc
from shannon.services.channels import ChannelMappingService
from shannon.services.sync.items import SyncOutcome, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration


async def test_two_events_for_a_new_pull_request_at_once_create_one_item(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """GitHub fires `opened` and `labeled` together for a pull request opened with labels.

    They carry different delivery ids, so the duplicate guard lets both through, and both find
    no tracked item yet.
    """
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())

    results = await asyncio.gather(
        service.sync(pr_event("opened")),
        service.sync(pr_event("labeled")),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"a concurrent delivery raised: {failures}"
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_two_events_for_a_new_issue_at_once_create_one_item(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    issue_event,
) -> None:
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, IssuePolicy())

    results = await asyncio.gather(
        service.sync(issue_event("opened")),
        service.sync(issue_event("assigned")),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"a concurrent delivery raised: {failures}"
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_a_burst_of_deliveries_still_leaves_one_item(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    pr_event,
) -> None:
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())

    results = await asyncio.gather(
        *(service.sync(pr_event("edited", title=f"Title {n}")) for n in range(6)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"a concurrent delivery raised: {failures}"
    assert all(r.outcome is SyncOutcome.SYNCED for r in results)
    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 1


async def test_two_people_running_set_channel_at_once_leave_one_mapping(
    registered: Repository, db_sessionmaker: async_sessionmaker, db_session: AsyncSession
) -> None:
    """Both find nothing mapped, and a read-then-write would have both insert."""
    service = ChannelMappingService(db_sessionmaker)

    results = await asyncio.gather(
        *(
            service.assign(
                guild_id=registered.discord_guild_id,
                object_type=ObjectType.PR,
                channel_id=channel,
            )
            for channel in (100, 200, 300)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"a concurrent /set_channel raised: {failures}"
    mappings = (
        await db_session.scalars(
            select(ChannelMapping).where(ChannelMapping.object_type == ObjectType.PR)
        )
    ).all()
    assert len(mappings) == 1
    assert mappings[0].discord_channel_id in (100, 200, 300)


async def test_two_syncs_adding_the_same_reviewer_at_once_do_not_collide(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """Two syncs of an item that already exists, which nothing serialises.

    A new item is serialised by the upsert in `get_or_create`: the loser blocks on the
    tracked_items row and sees the winner's assignments. An existing item has no such gate, so
    both callers read no reviewer, both insert, and the unique constraint takes the loser's whole
    sync down. `/pr` is run precisely when a delivery for that item is in flight, so this shape
    is common.
    """
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), PullRequestPolicy())
    await service.sync(pr_event("opened", requested_reviewers=[]))

    item_id = await db_session.scalar(select(TrackedItem.id))
    assert item_id is not None

    async def request_the_same_reviewer(session: AsyncSession) -> None:
        await ItemAssignmentStore(session).replace(
            tracked_item_id=item_id,
            role=ActorRole.REVIEWER,
            actors=[Actor(login="monalisa", github_user_id=200)],
        )

    async with db_sessionmaker() as first, db_sessionmaker() as second:
        await first.begin()
        await second.begin()

        await request_the_same_reviewer(first)
        # Started while the first holds the row uncommitted, so it waits on the index rather
        # than racing by luck. This is the interleaving, made to happen rather than hoped for.
        loser = asyncio.create_task(request_the_same_reviewer(second))
        await asyncio.sleep(0.1)

        await first.commit()
        await loser
        await second.commit()

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ItemAssignment)
            .where(ItemAssignment.role_type == ActorRole.REVIEWER)
        )
    ) == 1


async def test_a_later_sync_cannot_push_the_high_water_mark_back_down(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """`github_updated_at` is what tells a late delivery from a current one.

    Nothing serialises two syncs of an item that exists, so both read the mark before either
    commits and whichever commits last writes from a read that is by then out of date. Carrying
    the older timestamp, it drags the mark back down, and the next genuinely late delivery reads
    as current. The lock step decides from this same field.

    Interleaved by hand. Gathering them proves nothing: once the first commits, the staleness
    guard turns the rest away before they reach the write.
    """
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), PullRequestPolicy())
    await service.sync(pr_event("opened", updated_at="2026-08-10T12:00:00Z"))

    item_id = await db_session.scalar(select(TrackedItem.id))

    async with db_sessionmaker() as ahead, db_sessionmaker() as behind:
        await ahead.begin()
        await behind.begin()

        # Both read the same mark, because neither has written yet.
        newer = await TrackedItemStore(ahead).get_by_id(item_id)
        older = await TrackedItemStore(behind).get_by_id(item_id)
        assert as_utc(newer.github_updated_at) == as_utc(older.github_updated_at)

        TrackedItemStore(ahead).raise_updated_at(newer, datetime(2026, 8, 10, 18, 0, tzinfo=UTC))
        await ahead.commit()

        # Carrying the older timestamp and a read that is now out of date, and committing last.
        TrackedItemStore(behind).raise_updated_at(older, datetime(2026, 8, 10, 13, 0, tzinfo=UTC))
        await behind.commit()

    db_session.expunge_all()
    stored = await db_session.scalar(select(TrackedItem.github_updated_at))
    assert as_utc(stored) == datetime(2026, 8, 10, 18, 0, tzinfo=UTC), (
        "an older sync pushed the mark back down, so the next late delivery reads as current"
    )


class TestAnItemThatWentAwayBetweenTwoStatements:
    """`get_or_create` inserts, and on a conflict reads back the row that won instead.

    Both statements take their own snapshot, so a repository unregistered in the moment between
    them leaves the insert doing nothing and the read finding nothing. Neither statement is
    wrong and the method has nothing to return.

    Simulated at the read rather than raced, because the two statements are inside one method
    call and nothing can be interleaved between them from outside. What is under test is what
    the caller is handed: `None` flowing on becomes an AttributeError several frames later with
    nothing in it naming the item, so this stops there and says which item it was.
    """

    async def test_it_says_which_item_rather_than_returning_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker
    ) -> None:
        class _RowIsGone(TrackedItemStore):
            async def get(self, **kwargs) -> None:
                return None

        async with db_sessionmaker() as session, session.begin():
            store = _RowIsGone(session)
            # A first call so the second one conflicts and takes the read-back path.
            await _create(TrackedItemStore(session))

            with pytest.raises(RuntimeError, match=f"{ObjectType.PR.value} {payloads.PR_ID}"):
                await _create(store)


async def test_a_sync_decides_it_is_current_only_once_nobody_else_is_writing(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """The staleness decision is made from one read and acted on several statements later.

    Read committed gives no snapshot stability inside a transaction, so with nothing held both
    syncs of an item read the mark before either commits, both answer "not superseded", and
    neither takes the stale exit. The one carrying the older payload then writes its whole
    snapshot over the newer one: title, state and priority, and the reviewers, whose rows
    `replace` deletes and reinserts with `notified_at` cleared, so somebody the newer payload had
    removed goes back on the item and is pinged for it a second time.

    Made to happen rather than hoped for, the way the collision above is: the row is held by
    somebody mid-write while the sync starts, so it waits on the row rather than on luck.
    """
    service = build_item_sync(db_sessionmaker, FakeThreadGateway(), PullRequestPolicy())
    snapshot = pr_event("opened")
    await service.sync(snapshot)
    newer = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    async with db_sessionmaker() as holder:
        await holder.begin()
        store = TrackedItemStore(holder)
        held = await store.get(
            repository_id=registered.id,
            object_type=ObjectType.PR,
            github_object_id=snapshot.github_object_id,
            lock=True,
        )
        assert held is not None
        store.raise_updated_at(held, newer)
        await holder.flush()

        # The same delivery arriving again, which is what a retry is, while somebody newer is
        # part way through writing.
        catching_up = asyncio.create_task(service.sync(snapshot))
        await asyncio.sleep(0.2)
        assert not catching_up.done(), "nothing overlapped, so this proves nothing"

        await holder.commit()
        result = await catching_up

    assert result.outcome is SyncOutcome.STALE, "it decided from what it read before the write"
    db_session.expire_all()
    item = await db_session.scalar(select(TrackedItem))
    assert as_utc(item.github_updated_at) == newer, "the older payload wrote over the newer one"


async def _create(store: TrackedItemStore) -> TrackedItem:
    return await store.get_or_create(
        repository_id=1,
        object_type=ObjectType.PR,
        github_object_id=payloads.PR_ID,
        github_object_number=7,
        github_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7",
        title="Add the webhook endpoint",
        github_state="open",
        status=Status.NOT_REVIEWED,
    )
