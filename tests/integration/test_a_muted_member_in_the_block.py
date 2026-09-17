"""A member who turned their pings off is still in the block, and the block does not ring them.

Issue #80. The metadata block is the one message that names everybody on an item, and on the
delivery that opens a thread it is a real message, so it notifies every account it mentions. What
a muted member asked for is to stop being notified without disappearing from the thread, so the
mention stays exactly where it was and the message carries a list of who it is allowed to reach.

Asserted on the fake gateway's record of that list rather than on the content, because the content
is identical either way and that is the point.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

ALICE = 555
BOB = 444


async def link(session: AsyncSession, login: str, account: int, discord_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord_id
    )
    await session.commit()


def allow_lists(threads: FakeThreadGateway) -> list[tuple]:
    return [(kind, notify) for kind, _, _, notify in threads.allowed]


async def test_the_block_names_a_muted_member_as_a_mention(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """Not as plain text. Turning pings off is not the same as leaving the item, and a thread
    that stopped saying who the reviewer is would be worse than the notification ever was."""
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()

    result = await sync_service.sync(pr_event("opened"))

    assert f"<@{ALICE}>" in threads.metadata_of(result.thread_id)


async def test_and_the_block_says_it_may_not_notify_them(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()

    await sync_service.sync(pr_event("opened"))

    assert allow_lists(threads) == [("create", ())]


async def test_somebody_who_did_not_mute_is_still_on_the_list(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """A mixed item is the ordinary one. One person's choice must not take the other's ping."""
    await link(db_session, "monalisa", 200, ALICE)
    await link(db_session, "hubot", 100, BOB)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()

    await sync_service.sync(pr_event("opened"))

    assert allow_lists(threads) == [("create", (BOB,))]


async def test_a_block_with_nobody_to_mention_says_nobody_may_be_notified(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """The `/refresh` wiring, which renders people in plain text so a backlog is not news.

    An empty list rather than no list at all, which are different answers: no list leaves the
    client's own rule in force and would notify anybody the content happened to name. So the
    command's promise now rests on two facts read off one switch instead of on the rendering
    alone, and neither can be undone without the other noticing.
    """
    await link(db_session, "monalisa", 200, ALICE)
    quiet = build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), mentions=False)

    result = await quiet.sync(pr_event("opened"))

    assert f"<@{ALICE}>" not in threads.metadata_of(result.thread_id)
    assert allow_lists(threads) == [("create", ())]


async def test_a_replacement_block_still_carries_a_list(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """A thread opened to replace a deleted one renders people in plain text, so the list applies
    to nothing there. It goes along anyway: an allow-list permits and does not force, and
    narrowing it on that branch would give one outcome two owners."""
    await link(db_session, "monalisa", 200, ALICE)
    first = await sync_service.sync(pr_event("opened"))
    await threads.delete(thread_id=first.thread_id)

    again = await sync_service.sync(pr_event("edited", title="Renamed"))

    assert again.created is True, "nothing was rebuilt, so this proves nothing"
    assert allow_lists(threads)[-1] == ("create", (ALICE,))


async def test_a_rewritten_metadata_message_carries_it_too(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """An edit notifies nobody, but the same call reposts the block when somebody deleted it, and
    a repost reaches everybody it names exactly as opening the thread did."""
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    await sync_service.sync(pr_event("opened"))

    await sync_service.sync(pr_event("edited", title="Renamed"))

    assert allow_lists(threads) == [("create", ()), ("update", ())]


async def test_nothing_about_the_rendering_changed(
    registered: Repository,
    sync_service: ItemSyncService,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    pr_event,
) -> None:
    """Muting somebody must not change one character of what the thread says, or the whole
    argument for keeping the mention falls over."""
    await link(db_session, "monalisa", 200, ALICE)
    loud = await sync_service.sync(pr_event("opened"))
    said = threads.metadata_of(loud.thread_id)

    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    await sync_service.sync(pr_event("edited", title="Renamed"))

    assert (
        threads.metadata_of(loud.thread_id).replace("Renamed", "Add the webhook endpoint") == said
    )
