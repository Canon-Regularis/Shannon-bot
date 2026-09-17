"""The line asking somebody for a review still names a muted member, and does not ring them.

Issue #80. This line exists because every metadata block after the first is an edit and an edit
notifies nobody, so somebody put on an item after its thread was opened would otherwise never
hear about it. For a member who has turned their pings off it is still worth posting: it is the
only visible record in the thread that they were added, and they asked not to be rung rather than
to be left out.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.team_links import TeamLinkStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.formatting import format_reviewer_ping, format_team_ping
from shannon.domain.enums import ActorRole
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.sync.items import ItemSyncService, build_item_sync
from shannon.services.sync.notifications import ActorNotifier
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

ALICE = 555
BOB = 444
ROLE = 777000
BEFORE = "2026-08-10T11:00:00Z"


async def link(session: AsyncSession, login: str, account: int, discord_id: int) -> None:
    await UserLinkStore(session).link(
        guild_id=1, github_username=login, github_user_id=account, discord_user_id=discord_id
    )
    await session.commit()


def notifying(sessionmaker: async_sessionmaker, threads: FakeThreadGateway) -> ItemSyncService:
    """The reviewer notifier as the container assembles it, with the mute seam turned on."""
    return build_item_sync(
        sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            sessionmaker,
            threads,
            role=ActorRole.REVIEWER,
            render=format_reviewer_ping,
            muted=MutedMemberStore,
        ),
    )


def lines(threads: FakeThreadGateway) -> list[tuple[str, tuple]]:
    return [(content, notify) for kind, _, content, notify in threads.allowed if kind == "post"]


async def opened_with_nobody_asked(service: ItemSyncService, pr_event) -> None:
    """The thread already open, so the ask that follows it is a line rather than the block."""
    await service.sync(pr_event("opened", requested_reviewers=[], updated_at=BEFORE))


async def test_the_line_names_them_and_notifies_nobody(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    service = notifying(db_sessionmaker, threads)
    await opened_with_nobody_asked(service, pr_event)

    await service.sync(pr_event("review_requested"))

    assert lines(threads) == [(f"Review requested from <@{ALICE}>.", ())]


async def test_the_line_is_still_posted(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    """Said on its own, because dropping it is the obvious tidy-up and it is the wrong one. The
    line is the only sign anybody reading the thread gets that somebody was asked after it was
    opened, and the block that would otherwise have said so is an edit nobody sees."""
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    service = notifying(db_sessionmaker, threads)
    await opened_with_nobody_asked(service, pr_event)

    result = await service.sync(pr_event("review_requested"))

    assert result.notified == ("monalisa",)
    assert len(threads.posts) == 1


async def test_somebody_who_did_not_mute_is_still_on_the_list(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    await link(db_session, "monalisa", 200, ALICE)
    await link(db_session, "hubot", 100, BOB)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    service = notifying(db_sessionmaker, threads)
    await opened_with_nobody_asked(service, pr_event)

    await service.sync(
        pr_event(
            "review_requested",
            requested_reviewers=[payloads.user("monalisa", 200), payloads.user("hubot", 100)],
        )
    )

    assert lines(threads)[0][1] == (BOB,)


async def test_a_notifier_built_without_the_seam_says_nothing_about_who_may_be_pinged(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    """None, not an empty list. They are different answers: None leaves the client's own rule in
    force, which is what a notifier whose content holds no account mentions wants."""
    await link(db_session, "monalisa", 200, ALICE)
    service = build_item_sync(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            db_sessionmaker, threads, role=ActorRole.REVIEWER, render=format_reviewer_ping
        ),
    )
    await opened_with_nobody_asked(service, pr_event)

    await service.sync(pr_event("review_requested"))

    assert lines(threads) == [(f"Review requested from <@{ALICE}>.", None)]


async def test_a_team_review_still_pings_the_role(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
) -> None:
    """The limitation, pinned. A role mention reaches everybody holding the role and Discord has
    no way to leave one person out of one, so a muted member in a linked role is still reached.

    The team notifier is built without the mute seam, so its line says nothing about who may be
    notified and the client's `roles=True` applies. Filtering its ids would have been harmless,
    since a role id could never be in `muted_members`, which is exactly why it is worth a test
    saying what it does instead of an argument about why it would have been fine.
    """
    await TeamLinkStore(db_session).link(guild_id=1, github_team="backend", discord_role_id=ROLE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    service = build_item_sync(
        db_sessionmaker,
        threads,
        PullRequestPolicy(),
        ActorNotifier(
            db_sessionmaker,
            threads,
            role=ActorRole.REVIEWER_TEAM,
            render=format_team_ping,
            mentions=TeamLinkStore,
            the_block_pings_them=False,
        ),
    )
    payload = payloads.pull_request_event("review_requested", requested_reviewers=[])
    payload["pull_request"]["requested_teams"] = [{"slug": "backend"}]
    snapshot = parse_pull_request_event("review_requested", payload)
    assert snapshot is not None

    await service.sync(snapshot)

    assert lines(threads) == [(f"Review requested from <@&{ROLE}>.", None)]


async def test_the_block_that_opens_a_thread_still_spends_the_claim_for_a_muted_member(
    registered: Repository,
    db_sessionmaker: async_sessionmaker,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    pr_event,
) -> None:
    """Issue #81's rule, checked against this one. The block named them and did not ring them, so
    a line beside it would do the same, and the claim is spent either way. Leaving it unspent
    would put the line out on the very next delivery, which is the bug #81 exists for."""
    await link(db_session, "monalisa", 200, ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)
    await db_session.commit()
    service = notifying(db_sessionmaker, threads)

    opened = await service.sync(pr_event("opened"))
    later = await service.sync(pr_event("labeled"))

    assert opened.notified == ()
    assert later.notified == ()
    assert threads.posts == [], "the ping moved one delivery later instead of going away"
