"""Moving the threads a changed channel mapping left behind. Issue #78.

Two tests here carry the change and both fail without it.

`test_the_signpost_goes_in_before_the_lock` is the ordering one: posting to an archived thread
unarchives it, so shutting first is undone by the very line meant to close the thread.

`test_a_refused_replacement_leaves_the_item_on_the_thread_it_had` is the one that matters. Every
other failure here costs a signpost; that one would cost an item its only thread.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.container import _relocation
from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.panels import Panel
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.services.sync.items import SyncOutcome, SyncResult, build_item_sync
from shannon.services.sync.policies import IssuePolicy, PullRequestPolicy
from shannon.services.sync.relocation import Mirror, ThreadRelocation
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

REPO = RepositorySnapshot(
    github_repo_id=payloads.REPO_ID,
    owner=payloads.OWNER,
    name=payloads.REPO,
    html_url=f"https://github.com/{payloads.OWNER}/{payloads.REPO}",
)
KEY = REPO.full_name.lower()
# Where `registered` puts them: pull requests in 99, issues in 98.
OLD_ISSUES = 98
NEW = 4242


def an_issue(number: int = 12, **overrides) -> IssueSnapshot:
    return replace(
        IssueSnapshot(
            repository=REPO,
            github_object_id=800_000 + number,
            number=number,
            title=f"Issue {number}",
            html_url=f"https://github.com/{REPO.full_name}/issues/{number}",
            state="open",
            author=Actor("octocat"),
        ),
        **overrides,
    )


def a_pull_request(number: int = 7, **overrides) -> PullRequestSnapshot:
    return replace(
        PullRequestSnapshot(
            repository=REPO,
            github_object_id=700_000 + number,
            number=number,
            title=f"Pull request {number}",
            html_url=f"https://github.com/{REPO.full_name}/pull/{number}",
            state="open",
            author=Actor("octocat"),
        ),
        **overrides,
    )


def github_with(*, pulls=(), issues=()) -> FakeGitHubClient:
    return FakeGitHubClient(
        repositories={KEY: REPO},
        pull_requests={(KEY, item.number): item for item in pulls},
        issues={(KEY, item.number): item for item in issues},
    )


def relocation_with(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
    *,
    cap: int = 10,
    asked: int = 40,
) -> ThreadRelocation:
    """Built the way the container builds it: relocating, and with no notifier."""
    return ThreadRelocation(
        sessionmaker,
        threads,
        mirrors={
            ObjectType.PR: Mirror(
                service=build_item_sync(sessionmaker, threads, PullRequestPolicy(), relocates=True),
                fetch=github.get_pull_request,
            ),
            ObjectType.ISSUE: Mirror(
                service=build_item_sync(sessionmaker, threads, IssuePolicy(), relocates=True),
                fetch=github.get_issue,
            ),
        },
        cap=cap,
        asked=asked,
    )


async def remap(
    session: AsyncSession, repository: Repository, object_type: ObjectType, *, channel_id: int
) -> None:
    """What `/set_channel` leaves behind, upserting the way the real store does.

    `tests.support.db.map_channel` inserts, which is right for a kind nobody has mapped and wrong
    for every test here: this file is about a mapping being CHANGED.
    """
    await ChannelMappingStore(session).set(
        repository_id=repository.id, object_type=object_type, discord_channel_id=channel_id
    )
    await session.commit()


async def strand_an_issue(
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    snapshot: IssueSnapshot,
) -> int:
    """Mirror an issue into the channel it is mapped to now, which a remap will orphan."""
    service = build_item_sync(sessionmaker, threads, IssuePolicy())
    result = await service.sync(snapshot)
    assert result.thread_id is not None
    return result.thread_id


async def row_for(session: AsyncSession, number: int) -> TrackedItem:
    item = await session.scalar(
        select(TrackedItem).where(TrackedItem.github_object_number == number)
    )
    assert item is not None
    await session.refresh(item)
    return item


class TestMovingAStrandedThread:
    async def test_the_item_gets_a_thread_in_the_channel_that_is_mapped_now(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[snapshot])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed, outcome.left) == (1, 0, 0)
        assert threads.created[-1].channel_id == NEW
        row = await row_for(db_session, snapshot.number)
        assert row.discord_thread_id == threads.created[-1].thread_id
        assert row.discord_thread_id != old
        assert row.discord_channel_id == NEW

    async def test_the_signpost_goes_in_before_the_lock(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Posting reopens an archived thread, so shutting first would be undone by the very line
        meant to close it. The order is the feature, not an implementation detail.
        """
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        posted_before = len(threads.posts)

        await relocation_with(db_sessionmaker, threads, github_with(issues=[snapshot])).relocate(
            guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW
        )

        said = [body for thread_id, body in threads.posts[posted_before:] if thread_id == old]
        assert said and said[0].startswith("-# This item is now mirrored in <#")
        assert f"<#{threads.created[-1].thread_id}>" in said[0]
        old_thread = threads.threads[old]
        assert (old_thread.locked, old_thread.archived) == (True, True)

    async def test_it_says_nothing_about_a_lock_it_cannot_promise(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The line is written before the lock is attempted, so a server without Manage Threads
        would otherwise be told it cannot reply somewhere it can."""
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        await relocation_with(db_sessionmaker, threads, github_with(issues=[snapshot])).relocate(
            guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW
        )

        said = [body for thread_id, body in threads.posts if thread_id == old]
        assert "locked" not in said[-1]

    async def test_a_thread_already_in_the_right_channel_is_left_alone(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        snapshot = an_issue()
        await strand_an_issue(db_sessionmaker, threads, snapshot)
        opened = len(threads.created)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[snapshot])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=OLD_ISSUES)

        assert (outcome.moved, outcome.left) == (0, 0)
        assert len(threads.created) == opened

    async def test_nobody_is_pinged(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Rehousing somebody's thread is not a reason to notify them about it again."""
        snapshot = a_pull_request(reviewers=(Actor("monalisa"),))
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        await service.sync(snapshot)
        await remap(db_session, registered, ObjectType.PR, channel_id=NEW)
        posted = len(threads.posts)

        await relocation_with(db_sessionmaker, threads, github_with(pulls=[snapshot])).relocate(
            guild_id=1, object_type=ObjectType.PR, channel_id=NEW
        )

        # The signpost and nothing else.
        assert len(threads.posts) == posted + 1


class TestTheRowNeverPointsAtNothing:
    async def test_a_refused_replacement_leaves_the_item_on_the_thread_it_had(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The one failure that would cost an item its only thread rather than a signpost.

        The swap is one guarded UPDATE from the old id to the new, so a create that never
        succeeded leaves the row exactly as it was, and the old thread open and still being
        written to. A later run tries again.
        """
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        threads.fail_next_create = True

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[snapshot])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed, outcome.left) == (0, 1, 1)
        row = await row_for(db_session, snapshot.number)
        assert row.discord_thread_id == old, "the item was left mirrored nowhere"
        assert row.discord_channel_id == OLD_ISSUES
        assert threads.threads[old].locked is False, "it shut a thread still in use"


class TestWhenTheRowDoesNotRememberTheChannel:
    async def test_it_asks_discord_and_writes_the_answer_down(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Every thread claimed before that column existed. Guessing would abandon a working
        thread on no evidence; asking costs one call, and the answer is kept."""
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        row = await row_for(db_session, snapshot.number)
        row.discord_channel_id = None
        await db_session.commit()
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[snapshot])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert outcome.moved == 1
        assert threads.lookups == [old]

    async def test_one_that_was_in_the_right_place_all_along_is_settled_not_moved(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """And the answer is written down, so it stops being a candidate rather than costing a
        lookup on every run for ever."""
        snapshot = an_issue()
        await strand_an_issue(db_sessionmaker, threads, snapshot)
        row = await row_for(db_session, snapshot.number)
        row.discord_channel_id = None
        await db_session.commit()

        service = relocation_with(db_sessionmaker, threads, github_with(issues=[snapshot]))
        outcome = await service.relocate(
            guild_id=1, object_type=ObjectType.ISSUE, channel_id=OLD_ISSUES
        )

        assert (outcome.moved, outcome.left) == (0, 0)
        row = await row_for(db_session, snapshot.number)
        assert row.discord_channel_id == OLD_ISSUES

        asked = len(threads.lookups)
        await service.relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=OLD_ISSUES)
        assert threads.lookups[asked:] == [], "it asked again about an answer it already had"

    async def test_a_thread_discord_no_longer_has_is_let_go_of(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Not moved and not failed: the pointer was worthless, and the item gets a fresh thread
        in the right channel from whatever visits it next."""
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        row = await row_for(db_session, snapshot.number)
        row.discord_channel_id = None
        await db_session.commit()
        threads.threads.pop(old)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[snapshot])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed, outcome.left) == (0, 0, 0)
        row = await row_for(db_session, snapshot.number)
        assert row.discord_thread_id is None

    async def test_a_refused_lookup_is_a_failure_rather_than_a_guess(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Reading a refusal as "gone" would let go of a live pointer and open a second thread
        beside a working one, which is the failure this whole path exists to undo."""
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        row = await row_for(db_session, snapshot.number)
        row.discord_channel_id = None
        await db_session.commit()
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        threads.refuses_every_lookup = True

        with caplog.at_level(logging.WARNING):
            outcome = await relocation_with(
                db_sessionmaker, threads, github_with(issues=[snapshot])
            ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed, outcome.left) == (0, 1, 1)
        row = await row_for(db_session, snapshot.number)
        assert row.discord_thread_id == old
        assert "could not find out where" in caplog.text


class TestWhichKindsMove:
    async def test_another_kind_in_the_same_channel_is_untouched(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """`forget_channel` matches on the channel alone, which would strand pull requests
        sharing it. This matches on the kinds the mapping actually decides."""
        issue = an_issue()
        await strand_an_issue(db_sessionmaker, threads, issue)
        pull = a_pull_request()
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(pull)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[issue], pulls=[pull])
        ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert outcome.moved == 1
        assert (await row_for(db_session, pull.number)).discord_channel_id == 99

    async def test_a_kind_borrowing_this_channel_moves_with_it(
        self,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Issues fall back to the pull request channel, so pointing pull requests somewhere new
        moves where issue threads go too. Relocating only the named kind would reproduce this very
        bug for the other one, triggered by fixing it.
        """
        from tests.support.db import register_repository

        repository = await register_repository(db_session)
        issue = an_issue()
        await strand_an_issue(db_sessionmaker, threads, issue)
        # What `/set_channel pull requests` writes before it asks for the move. The sync resolves
        # the channel itself, so the two have to agree, and the command is what makes them.
        await remap(db_session, repository, ObjectType.PR, channel_id=NEW)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[issue])
        ).relocate(guild_id=1, object_type=ObjectType.PR, channel_id=NEW)

        assert outcome.moved == 1, "the borrowed kind was left behind"
        assert threads.created[-1].channel_id == NEW

    async def test_a_kind_with_a_channel_of_its_own_is_not_borrowing(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        issue = an_issue()
        await strand_an_issue(db_sessionmaker, threads, issue)

        outcome = await relocation_with(
            db_sessionmaker, threads, github_with(issues=[issue])
        ).relocate(guild_id=1, object_type=ObjectType.PR, channel_id=NEW)

        assert outcome.moved == 0, "it moved a kind that has its own mapping"


class TestTheCap:
    async def test_it_moves_no_more_than_the_cap_and_says_what_is_left(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        issues = [an_issue(12), an_issue(13), an_issue(14)]
        for snapshot in issues:
            await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        service = relocation_with(db_sessionmaker, threads, github_with(issues=issues), cap=2)
        first = await service.relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (first.moved, first.left) == (2, 1)

        second = await service.relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)
        assert (second.moved, second.left) == (1, 0)


class TestWhenOneItemFails:
    async def test_the_rest_still_move(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        issues = [an_issue(12), an_issue(13)]
        for snapshot in issues:
            await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        threads.fail_next_create = True

        with caplog.at_level(logging.WARNING):
            outcome = await relocation_with(
                db_sessionmaker, threads, github_with(issues=issues)
            ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed, outcome.left) == (1, 1, 1)
        assert "could not move the thread" in caplog.text

    async def test_a_surprise_does_not_strand_the_command(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        snapshot = an_issue()
        await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        github = github_with(issues=[snapshot])
        github.error = RuntimeError("something nobody predicted")

        with caplog.at_level(logging.ERROR):
            outcome = await relocation_with(db_sessionmaker, threads, github).relocate(
                guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW
            )

        assert (outcome.moved, outcome.failed) == (0, 1)
        assert "an unexpected failure moving" in caplog.text

    async def test_an_unregistered_server_is_told_to_register(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
    ) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await relocation_with(db_sessionmaker, threads, github_with()).relocate(
                guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW
            )


class TestTheOrdinaryDeliveryPath:
    async def test_it_never_moves_a_thread_between_channels(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Relocating is built into the binding rather than asked for per call, so a webhook
        cannot do it however far the mapping has drifted. A relocation is several Discord calls
        and leaves a thread somebody has to be told about, and the queue has nobody to tell.
        """
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)

        service = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        result = await service.sync(replace(snapshot, title="Edited since"))

        assert result.thread_id == old
        assert result.displaced is None
        assert "Edited since" in threads.metadata_of(old)


class TestTheWiring:
    async def test_the_container_builds_it_to_relocate_and_not_to_ping(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Both halves live in the wiring and neither is visible from the service, so nothing
        else in this file would notice if a later edit dropped one.

        Not pinging covers the block it posts as well as any line. A moved thread is the same
        item in a different place, and the block that opens one is a real message, so leaving
        the mentions in it would tell everybody on the item that something happened when the
        only thing that happened is somebody fixing a channel mapping.
        """
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=555
        )
        await db_session.commit()
        snapshot = a_pull_request(reviewers=(Actor("monalisa"),))
        await build_item_sync(db_sessionmaker, threads, PullRequestPolicy()).sync(snapshot)
        await remap(db_session, registered, ObjectType.PR, channel_id=NEW)
        posted = len(threads.posts)

        outcome = await _relocation(
            db_sessionmaker, github_with(pulls=[snapshot]), threads
        ).relocate(guild_id=1, object_type=ObjectType.PR, channel_id=NEW)

        assert outcome.moved == 1, "the wiring did not relocate"
        assert len(threads.posts) == posted + 1, "the wiring pinged somebody"
        moved_to = threads.created[-1].thread_id
        assert "<@" not in threads.metadata_of(moved_to), "the block it posted pinged somebody"


class TestABoardCard:
    """A draft card exists nowhere but the board and has no endpoint to fetch it by number, so
    there is no snapshot to build a replacement from.

    The order inverts: let go of the pointer first, and the poller opens the new thread on its
    next pass, because a card with no thread is exactly the state it already repairs.
    """

    async def a_card_in(
        self,
        session: AsyncSession,
        repository: Repository,
        threads: FakeThreadGateway,
        channel_id: int,
    ) -> int:
        handle = await threads.create(
            channel_id=channel_id, name="A card", panel=Panel.of_text("block")
        )
        session.add(
            TrackedItem(
                repository_id=repository.id,
                github_object_id=900_001,
                github_object_type=ObjectType.TICKET,
                github_object_number=0,
                github_url="https://github.com/users/x/projects/1",
                title="A card",
                github_state="open",
                discord_thread_id=handle.thread_id,
                discord_channel_id=channel_id,
            )
        )
        await session.commit()
        return handle.thread_id

    async def test_its_pointer_is_let_go_of_and_the_channel_named(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        old = await self.a_card_in(db_session, registered, threads, 500)

        outcome = await relocation_with(db_sessionmaker, threads, github_with()).relocate(
            guild_id=1, object_type=ObjectType.TICKET, channel_id=NEW
        )

        assert outcome.moved == 1
        card = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.TICKET)
        )
        await db_session.refresh(card)
        assert card.discord_thread_id is None, "the poller has nothing to rebuild"
        said = [body for thread_id, body in threads.posts if thread_id == old]
        assert said[-1] == (
            f"-# This item will be mirrored in <#{NEW}> from now on. "
            "Nothing more will be posted here."
        )
        assert threads.threads[old].archived is True


class TestWhenTheOldThreadCannotBeToldAnything:
    async def test_the_item_has_still_moved(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """By the time the signpost is attempted the row already points at the replacement, so a
        refusal costs the line and the lock on a thread nothing will write to again. Counting it
        as a failure would have a later run try to move an item that has already moved.
        """
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        threads.refuses_every_shut = True

        with caplog.at_level(logging.WARNING):
            outcome = await relocation_with(
                db_sessionmaker, threads, github_with(issues=[snapshot])
            ).relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert (outcome.moved, outcome.failed) == (1, 0)
        assert (await row_for(db_session, snapshot.number)).discord_channel_id == NEW
        assert "could not say so in it" in caplog.text
        assert threads.threads[old].locked is False


class TestWhenNothingWasDisplaced:
    async def test_there_is_no_old_thread_to_say_anything_in(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Something attached a thread in the right channel while this was in flight, or the sync
        turned the item away for a reason of its own. Either way nothing was left behind.
        """
        snapshot = an_issue()
        old = await strand_an_issue(db_sessionmaker, threads, snapshot)
        await remap(db_session, registered, ObjectType.ISSUE, channel_id=NEW)
        posted = len(threads.posts)

        class _MovedNothing:
            async def sync(self, snapshot, **_):
                return SyncResult(outcome=SyncOutcome.SYNCED, thread_id=old, displaced=None)

        github = github_with(issues=[snapshot])
        service = ThreadRelocation(
            db_sessionmaker,
            threads,
            mirrors={ObjectType.ISSUE: Mirror(service=_MovedNothing(), fetch=github.get_issue)},
        )

        outcome = await service.relocate(guild_id=1, object_type=ObjectType.ISSUE, channel_id=NEW)

        assert outcome.moved == 1
        assert threads.posts[posted:] == [], "it signposted a thread nothing left"
