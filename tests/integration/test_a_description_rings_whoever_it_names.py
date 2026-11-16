"""An issue or a pull request that @names somebody in its description rings them. Issue #259.

A comment that names somebody rang them (#231), and a description did not: the block that opens the
thread showed `@a-dish12` and `@matthew831709` as plain text, and only the Author, Assignees and
Reviewers rows rang. The block is already the one real message that rings everybody on an item, so
the names join its list rather than getting a message of their own.

Three things keep it from ringing anybody who should not be. Only the message that opens a thread
rings at all, and an edit rings nobody. Only an item opened in the last day rings for its
description, because any delivery for an item with no row opens a thread, and a year-old issue first
seen today should not wake people over something written a year ago. And every rule that already
keeps the rows quiet - a link with no proof, a muted member, a login that changed hands, a rebuild
from an old delivery, a replacement thread - keeps the description quiet too.

Asserted on the fake gateway's record of what each write said and who it was allowed to reach,
because the question is always both: whether the name shows as a mention, and whether it rings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import Notify
from shannon.domain.models import TrackedSnapshot
from shannon.github.webhooks.issues import parse_issue_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.items import DESCRIBED_RINGS_FOR, ItemSyncService, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy, SyncPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

# The two names in the report, and the Discord accounts they are linked to.
DISH = 111
MATTHEW = 222
BOTH = "Could @a-dish12 and @matthew831709 check the figures?"

# The assignee both payload builders put on an item.
HUBOT = 333

# When the item was opened, and the clock the sync reads: an hour later, well inside the day.
OPENED = "2026-08-11T09:00:00Z"
NOW = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
OPENED_LONG_AGO = "2025-08-11T09:00:00Z"

# Three moments for the rebuild, in the order GitHub would have sent them.
BEFORE = "2026-08-11T09:10:00Z"
STALE = "2026-08-11T09:20:00Z"
AFTER = "2026-08-11T09:30:00Z"


def issue(action: str = "opened", **overrides: object) -> TrackedSnapshot:
    snapshot = parse_issue_event(action, payloads.issue_event(action, **overrides))
    assert snapshot is not None
    return snapshot


def pr(action: str = "opened", **overrides: object) -> TrackedSnapshot:
    snapshot = parse_pull_request_event(action, payloads.pull_request_event(action, **overrides))
    assert snapshot is not None
    return snapshot


def sync_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    policy: SyncPolicy,
    *,
    mentions: bool = True,
    notifies: bool = True,
    now: datetime = NOW,
) -> ItemSyncService:
    """What a delivery is synced by, with the clock held still so an item's age is exact."""
    return build_item_sync(
        sessionmaker, threads, policy, mentions=mentions, notifies=notifies, now=lambda: now
    )


async def link(
    session: AsyncSession, login: str, discord_id: int, *, account: int | None = 1
) -> None:
    """A proved link unless `account` says otherwise. A body name carries no id to check one
    against, so any proved id resolves it; None is a link typed before `/link` asked GitHub."""
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord_id
    )
    await session.commit()


async def link_both(session: AsyncSession) -> None:
    await link(session, "a-dish12", DISH)
    await link(session, "matthew831709", MATTHEW)


def last(threads: FakeThreadGateway) -> tuple[str, str, Notify]:
    """What the last write to Discord was, what it said, and who it was allowed to reach."""
    kind, _, content, notify = threads.allowed[-1]
    return kind, content, notify


class TestAThreadOpenedForANewItem:
    """The report, and the ordinary case: one message, both names live, both on its list."""

    async def test_an_issue_rings_both_people_its_description_names(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        assert len(threads.created) == 1
        thread = threads.created[0]
        assert list(thread.messages.values()) == [threads.metadata_of(thread.thread_id)]
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in threads.metadata_of(thread.thread_id)
        assert [(kind, notify) for kind, _, _, notify in threads.allowed] == [
            ("create", (DISH, MATTHEW))
        ]

    async def test_a_pull_request_rings_them_the_same_way(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, PullRequestPolicy()).sync(
            pr(created_at=OPENED, body=BOTH)
        )

        assert len(threads.created) == 1
        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content
        assert (kind, notify) == ("create", (DISH, MATTHEW))

    async def test_somebody_named_and_assigned_is_rung_once(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """One message rings each person once, however many times it names them. The Assignees
        row and the description both show the mention, and the list carries the account once."""
        await link(db_session, "hubot", HUBOT, account=100)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=OPENED, body="over to @hubot")
        )

        kind, content, notify = last(threads)
        assert content.count(f"<@{HUBOT}>") == 2
        assert (kind, notify) == ("create", (HUBOT,))

    async def test_a_labeled_delivery_right_after_adds_nothing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """GitHub sends a `labeled` beside the `opened` for every label an item was created with
        (#81). It finds the thread there and edits the block, and an edit rings nobody."""
        await link_both(db_session)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())

        await sync.sync(issue(created_at=OPENED, updated_at=BEFORE, body=BOTH))
        await sync.sync(issue("labeled", created_at=OPENED, updated_at=AFTER, body=BOTH))

        assert len(threads.created) == 1
        assert threads.posts == []
        assert [kind for kind, _, _, _ in threads.allowed] == ["create", "update"]

    async def test_a_labeled_delivery_that_beats_opened_still_rings(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The worker does not promise the order GitHub sent them in, so the thread can be opened
        by the `labeled` one. Whether names ring is read off the item's age, not off the action,
        which is what keeps this the same as the ordinary order."""
        await link_both(db_session)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())

        await sync.sync(issue("labeled", created_at=OPENED, body=BOTH))
        await sync.sync(issue(created_at=OPENED, body=BOTH))

        assert len(threads.created) == 1
        kind, _, content, notify = threads.allowed[0]
        assert f"<@{DISH}>" in content
        assert (kind, notify) == ("create", (DISH, MATTHEW))


class TestAnItemThatIsNotNew:
    """The first activity on an item from before `/register` opens its thread too. The names in
    its description were written long ago, so they show as mentions and ring nobody; the people on
    the item are rung exactly as they were before this change."""

    async def test_an_old_issue_first_seen_on_a_label_names_them_without_ringing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        await link(db_session, "hubot", HUBOT, account=100)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue("labeled", created_at=OPENED_LONG_AGO, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content, "names are still shown live"
        assert (kind, notify) == ("create", (HUBOT,)), "only the assignee rang, as before #259"

    async def test_an_old_pull_request_first_seen_on_a_push_names_them_without_ringing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        await link(db_session, "hubot", HUBOT, account=100)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, PullRequestPolicy()).sync(
            pr("synchronize", created_at=OPENED_LONG_AGO, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"<@{DISH}>" in content
        assert (kind, notify) == ("create", (HUBOT,))

    async def test_an_item_already_open_shows_them_live_on_its_next_edit(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The issue in #259 itself, and every other thread open before this change. Its names
        turn into mentions the next time the block is written, and that write is an edit."""
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())
        await sync.sync(issue(created_at=OPENED_LONG_AGO, updated_at=BEFORE, body=BOTH))
        assert "<@" not in last(threads)[1], "nobody was linked yet, so this proves nothing"
        await link_both(db_session)

        await sync.sync(issue("edited", created_at=OPENED_LONG_AGO, updated_at=AFTER, body=BOTH))

        assert len(threads.created) == 1
        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content
        assert (kind, notify) == ("update", ())

    async def test_an_item_opened_exactly_a_day_ago_still_rings(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        threads = FakeThreadGateway()
        opened = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)

        await sync_for(
            db_sessionmaker, threads, IssuePolicy(), now=opened + DESCRIBED_RINGS_FOR
        ).sync(issue(created_at=OPENED, body=BOTH))

        assert last(threads)[2] == (DISH, MATTHEW)

    async def test_a_second_later_it_does_not(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        threads = FakeThreadGateway()
        opened = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)

        await sync_for(
            db_sessionmaker,
            threads,
            IssuePolicy(),
            now=opened + DESCRIBED_RINGS_FOR + timedelta(seconds=1),
        ).sync(issue(created_at=OPENED, body=BOTH))

        assert last(threads)[2] == ()

    async def test_an_item_that_does_not_say_when_it_was_opened_rings_nobody(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """No date is no evidence the names are news, and a ring cannot be taken back."""
        await link_both(db_session)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=None, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"<@{DISH}>" in content
        assert (kind, notify) == ("create", ())


class TestANameNobodyCanBeRungFor:
    """The rules that keep a row quiet keep the description quiet, because it is one list."""

    async def test_an_unlinked_name_stays_as_written(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link(db_session, "a-dish12", DISH)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and @matthew831709 check" in content
        assert (kind, notify) == ("create", (DISH,))

    async def test_a_link_with_no_github_id_stays_as_written(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """A link typed before `/link` asked GitHub whose the login was is a claim, and a claim
        is not enough to ring anybody (#135)."""
        await link(db_session, "a-dish12", DISH)
        await link(db_session, "matthew831709", MATTHEW, account=None)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert "@matthew831709 check" in content
        assert f"<@{MATTHEW}>" not in content
        assert (kind, notify) == ("create", (DISH,))

    async def test_a_muted_member_is_named_and_not_rung(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """Turning pings off is not leaving the thread (#80), so the mention stays where it was."""
        await link_both(db_session)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=MATTHEW)
        await db_session.commit()
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content
        assert (kind, notify) == ("create", (DISH,))

    async def test_an_author_whose_login_changed_hands_naming_themselves_is_not_resolved(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The order of the people map, held to its reason. The author is `OctoCat` on GitHub and
        lowered in the body, so both names reach the lookup; the payload's id has to win over the
        body's none, or writing your own name would let the old holder's link ring a stranger."""
        await link(db_session, "octocat", 909, account=777)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(
                created_at=OPENED,
                body="as @octocat said",
                user=payloads.user("OctoCat", 583231),
            )
        )

        kind, content, notify = last(threads)
        assert "<@909>" not in content, "a stranger inherited somebody else's mention"
        assert (kind, notify) == ("create", ())

    async def test_the_same_author_with_the_id_that_matches_is_rung(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The control. Without it, the test above passes on a build that resolves nobody."""
        await link(db_session, "octocat", 909, account=583231)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy()).sync(
            issue(
                created_at=OPENED,
                body="as @octocat said",
                user=payloads.user("OctoCat", 583231),
            )
        )

        kind, content, notify = last(threads)
        assert "as <@909> said" in content
        assert (kind, notify) == ("create", (909,))


class TestAWriteThatRingsNobody:
    """Every path that names people without ringing them already did so for the rows, and the
    description follows: it is the same block and the same list."""

    async def test_an_edit_that_adds_a_name_shows_it_and_rings_nobody(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """The same as an edited comment (#165). The block is edited in place, and Discord rings
        nobody for an edit whatever it says."""
        await link_both(db_session)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())

        await sync.sync(issue(created_at=OPENED, updated_at=BEFORE, body="Nobody named yet."))
        await sync.sync(issue("edited", created_at=OPENED, updated_at=AFTER, body=BOTH))

        assert len(threads.created) == 1
        assert threads.posts == []
        kind, _, _, notify = threads.allowed[0]
        assert (kind, notify) == ("create", ())
        kind, content, _ = last(threads)
        assert kind == "update"
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content

    async def test_a_block_posted_again_in_its_own_thread_does_not_ring_them_twice(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """Somebody deleted the block, and the next write posts a new one into the same thread
        with the list it is handed. The opening block already rang the names; only the people on
        the item stay on the list, as they were before #259."""
        await link_both(db_session)
        await link(db_session, "hubot", HUBOT, account=100)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())
        first = await sync.sync(issue(created_at=OPENED, updated_at=BEFORE, body=BOTH))
        assert first.thread_id is not None
        assert sorted(last(threads)[2] or ()) == [DISH, MATTHEW, HUBOT], "nobody rang to begin with"
        thread = threads.threads[first.thread_id]
        assert thread.metadata_message_id is not None
        threads.forget_message(first.thread_id, thread.metadata_message_id)

        await sync.sync(issue("labeled", created_at=OPENED, updated_at=AFTER, body=BOTH))

        assert len(threads.created) == 1
        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content
        assert (kind, notify) == ("update", (HUBOT,))

    async def test_a_sync_that_does_not_notify_names_them_and_rings_nobody(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """How `/refresh` and `/regenerate` are built (#185)."""
        await link_both(db_session)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy(), notifies=False).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert f"Could <@{DISH}> and <@{MATTHEW}> check" in content
        assert (kind, notify) == ("create", ())

    async def test_a_sync_without_mentions_names_them_in_plain_text(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        await link_both(db_session)
        threads = FakeThreadGateway()

        await sync_for(db_sessionmaker, threads, IssuePolicy(), mentions=False).sync(
            issue(created_at=OPENED, body=BOTH)
        )

        kind, content, notify = last(threads)
        assert BOTH in content
        assert "<@" not in content
        assert (kind, notify) == ("create", ())

    async def test_a_rebuild_from_an_old_delivery_names_them_in_plain_text(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """A thread that went is rebuilt however old the delivery is, and that delivery is not
        believed about the people on the item; nor is it about who its description names."""
        await link_both(db_session)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())

        await sync.sync(issue(created_at=OPENED, updated_at=BEFORE, body=BOTH))
        await sync.sync(issue("edited", created_at=OPENED, updated_at=AFTER, body=BOTH))
        item = await db_session.scalar(select(TrackedItem))
        assert item is not None and item.discord_thread_id is not None
        async with db_sessionmaker() as writing, writing.begin():
            await ThreadPointerStore(writing).forget_thread(
                item.id, dead_thread_id=item.discord_thread_id
            )

        await sync.sync(issue("edited", created_at=OPENED, updated_at=STALE, body=BOTH))

        assert len(threads.created) == 2, "nothing was rebuilt, so this proves nothing"
        kind, content, notify = last(threads)
        assert BOTH in content
        assert (kind, notify) == ("create", ())

    async def test_a_replacement_thread_names_them_in_plain_text(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """A thread opened to replace one somebody deleted is posted quiet, because the people it
        names were already told in the one it replaces."""
        await link_both(db_session)
        threads = FakeThreadGateway()
        sync = sync_for(db_sessionmaker, threads, IssuePolicy())
        first = await sync.sync(issue(created_at=OPENED, updated_at=BEFORE, body=BOTH))
        assert first.thread_id is not None
        await threads.delete(thread_id=first.thread_id)

        again = await sync.sync(issue("edited", created_at=OPENED, updated_at=AFTER, body=BOTH))

        assert again.created is True, "nothing was rebuilt, so this proves nothing"
        kind, content, _ = last(threads)
        assert kind == "create"
        assert BOTH in content
        assert "<@" not in content
