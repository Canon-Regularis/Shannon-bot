"""Naming somebody in the block and ringing them are two questions, and now two switches.

Issue #65. They used to be one. `mentions=False` turned off the `user_links` lookup, the rendering
and the allow-list together, which is right for a backlog mirror opening twenty-five threads at
once and wrong for the one caller that has to name people without waking any of them: a redraw of
an item that closed weeks ago.

Both services are driven side by side here on purpose. The interesting assertion is not what either
one does alone, it is that the block is character-for-character the same and only the allow-list
differs. Asserting on one service would pass with the flag doing nothing at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.user_links import UserLinkStore
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway

pytestmark = pytest.mark.integration

ALICE = 555


async def link(session: AsyncSession, login: str, account: int, discord_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord_id
    )
    await session.commit()


def written(threads: FakeThreadGateway) -> tuple[str, tuple | None]:
    """The content and the allow-list of the last thing that reached Discord."""
    _, _, content, notify = threads.allowed[-1]
    return content, notify


async def test_a_service_that_notifies_names_them_and_may_ring_them(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    pr_event,
) -> None:
    """The ordinary path, unchanged. Here as the control: without it, the test below would pass
    on a build where nobody is ever notified about anything."""
    await link(db_session, "monalisa", 200, ALICE)
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())

    await service.sync(pr_event("opened"))

    content, notify = written(threads)
    assert f"<@{ALICE}>" in content
    assert notify == (ALICE,)


async def test_a_service_that_does_not_notify_still_names_them(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    pr_event,
) -> None:
    """The whole of the new flag. The block is identical and nobody is reachable through it."""
    await link(db_session, "monalisa", 200, ALICE)
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), notifies=False)

    await service.sync(pr_event("opened"))

    content, notify = written(threads)
    assert f"<@{ALICE}>" in content, "the mention is the point; plain text is the old bug"
    assert notify == (), "an empty allow-list, not None: None leaves the client's own rule on"


async def test_the_two_write_the_same_block(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    pr_event,
) -> None:
    """Character for character. The flag decides who Discord may ring and nothing else, so a
    change that quietly altered the rendering as well would be caught here rather than by
    somebody noticing a thread reads differently.

    Two different items rather than one twice over. Syncing the same item through a second
    gateway finds the thread the first one opened recorded against it and missing from this one,
    which is the deleted-thread rebuild, and a rebuild deliberately renders people in plain text.
    That would have compared the redraw against the rebuild and called the flag broken.
    """
    await link(db_session, "monalisa", 200, ALICE)
    loud, quiet = FakeThreadGateway(), FakeThreadGateway()

    await build_item_sync(db_sessionmaker, loud, PullRequestPolicy()).sync(
        pr_event("opened", id=101, number=11)
    )
    await build_item_sync(db_sessionmaker, quiet, PullRequestPolicy(), notifies=False).sync(
        pr_event("opened", id=102, number=12)
    )

    # The number is in the block and in the thread name, so it is the one thing that legitimately
    # differs between two items. Everything else has to match.
    assert written(loud)[0].replace("#11", "#N") == written(quiet)[0].replace("#12", "#N")
    assert written(loud)[1] != written(quiet)[1]


async def test_mentions_off_still_means_plain_text_and_nobody(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    pr_event,
) -> None:
    """The existing switch, unchanged by the new one. `/refresh` depends on this: every thread it
    opens is a first block and a first block is posted, so twenty-five of them would notify
    everybody on all twenty-five about a backlog that has been sitting there."""
    await link(db_session, "monalisa", 200, ALICE)
    threads = FakeThreadGateway()
    service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy(), mentions=False)

    await service.sync(pr_event("opened"))

    content, notify = written(threads)
    assert f"<@{ALICE}>" not in content
    assert "monalisa" in content
    assert notify == ()


async def test_both_off_together_is_the_same_as_either(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    pr_event,
) -> None:
    """They are not a matrix anybody has to reason about: turning off the rendering already
    empties the allow-list, so the fourth combination is the third one again."""
    await link(db_session, "monalisa", 200, ALICE)
    threads = FakeThreadGateway()
    service = build_item_sync(
        db_sessionmaker, threads, PullRequestPolicy(), mentions=False, notifies=False
    )

    await service.sync(pr_event("opened"))

    content, notify = written(threads)
    assert f"<@{ALICE}>" not in content
    assert notify == ()
