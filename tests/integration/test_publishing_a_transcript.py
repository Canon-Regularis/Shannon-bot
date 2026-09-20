"""Getting a captured conversation onto GitHub, and what happens when that goes wrong.

Issue #103. The crash tests below are the point of this file, and one of them pins behaviour that
looks like a bug and is a decision: a process that dies between GitHub accepting the comment and
the rows being deleted publishes that batch a second time. A duplicate comment is visible and a
person can delete it. A transcript silently missing a chunk of what was said defeats the point of
keeping the rows at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import LoggedConversation, LoggedMessage, Repository, UserLink
from shannon.db.stores.conversations import ConversationStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.capture import CapturedMessage
from shannon.github.errors import GitHubRateLimitError
from shannon.services.transcripts.flush import (
    FLUSH_RETRY_AFTER,
    MOST_ATTEMPTS,
    TranscriptFlusher,
)
from shannon.services.transcripts.log import ConversationLog
from shannon.services.transcripts.publish import TranscriptPublisher
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.waiting import until

pytestmark = pytest.mark.integration

WHO = 4242
ALICE = 77
# Somebody else in the thread, who gets tagged rather than doing the talking. A real snowflake,
# because the token the render looks for insists on fifteen to twenty digits.
BOB = 222222222222222222
AT = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
QUIET = timedelta(seconds=60)
FULL_NAME = f"{payloads.OWNER}/{payloads.REPO}".lower()


@pytest.fixture
def clock() -> list[datetime]:
    return [AT]


@pytest.fixture
def github(pr_event) -> FakeGitHubClient:
    return FakeGitHubClient(pull_requests={(FULL_NAME, 7): pr_event("opened")})


@pytest.fixture
def log(
    db_sessionmaker: async_sessionmaker[AsyncSession], threads: FakeThreadGateway
) -> ConversationLog:
    return ConversationLog(db_sessionmaker, threads)


@pytest.fixture
def flusher(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    clock: list[datetime],
) -> TranscriptFlusher:
    return TranscriptFlusher(
        db_sessionmaker,
        TranscriptPublisher(github),
        threads,
        quiet_gap=QUIET,
        tick=timedelta(seconds=5),
        now=lambda: clock[0],
    )


@pytest.fixture
async def logging_thread(log: ConversationLog, thread_id: int) -> int:
    await log.start(thread_id=thread_id, by=WHO)
    return thread_id


async def say(
    log: ConversationLog,
    thread_id: int,
    *what: str,
    minute: int = 0,
    mentions: Mapping[int, str] | None = None,
) -> None:
    for offset, content in enumerate(what):
        await log.capture(
            CapturedMessage(
                thread_id=thread_id,
                message_id=500 + offset,
                author_id=ALICE,
                author_display_name="alice",
                content=content,
                said_at=AT + timedelta(minutes=minute),
                mentions=mentions or {},
            )
        )


async def conversation(session: AsyncSession) -> LoggedConversation:
    row = await session.scalar(select(LoggedConversation))
    assert row is not None
    return row


async def waiting(session: AsyncSession) -> int:
    return len(list(await session.scalars(select(LoggedMessage))))


class TestTheOrdinaryPath:
    async def test_a_quiet_thread_is_published_as_one_comment(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        clock: list[datetime],
    ) -> None:
        """One comment rather than one per message, which is the whole of why anything is
        buffered: ten messages published one at a time would bury the item and email everybody
        watching it ten times."""
        await say(log, logging_thread, "got the repro", "it is the label cache")
        clock[0] = AT + QUIET

        await flusher.flush_once()

        assert len(github.comments) == 1
        where, number, body = github.comments[0]
        assert (where, number) == (FULL_NAME, 7)
        assert "got the repro" in body
        assert "it is the label cache" in body

    async def test_what_was_published_is_no_longer_waiting(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        await say(log, logging_thread, "one")
        clock[0] = AT + QUIET

        await flusher.flush_once()

        db_session.expire_all()
        assert await waiting(db_session) == 0
        row = await conversation(db_session)
        assert (row.flush_id, row.flush_through_id, row.failed_flushes) == (None, None, 0)

    async def test_a_thread_still_being_talked_in_waits(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
    ) -> None:
        await say(log, logging_thread, "one")

        await flusher.flush_once()

        assert github.comments == []

    async def test_stopping_publishes_the_tail_at_once(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
    ) -> None:
        """Ending the logging is what gets the rest of the conversation onto GitHub, not what
        throws it away. No quiet gap is waited out: somebody has said they are done."""
        await say(log, logging_thread, "last thing")
        await log.stop(thread_id=logging_thread, by=WHO)

        await flusher.flush_once()

        assert len(github.comments) == 1

    async def test_nothing_waiting_anywhere_publishes_nothing(
        self, flusher: TranscriptFlusher, github: FakeGitHubClient
    ) -> None:
        await flusher.flush_once()

        assert github.comments == []

    async def test_a_message_landing_mid_flush_is_left_for_the_next_batch(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        clock: list[datetime],
    ) -> None:
        """The claim names how far along the rows it reaches, and this is the case that says why.

        Somebody carries on typing while the comment is in flight to GitHub. That message is not in
        the body being sent, so deleting it with the batch would lose it with nothing saying so.

        Written as a message arriving DURING the publish rather than after it. An earlier version
        of this test added it afterwards, which exercises nothing: widening the delete to cover
        everything went green, because by then there was nothing newer to destroy.
        """
        await say(log, logging_thread, "first")
        clock[0] = AT + QUIET
        published = TranscriptPublisher(github).publish

        async def publish_while_somebody_carries_on_typing(found, lines):
            await log.capture(
                CapturedMessage(
                    thread_id=logging_thread,
                    message_id=999,
                    author_id=ALICE,
                    author_display_name="alice",
                    content="second",
                    said_at=AT + timedelta(minutes=5),
                    mentions={},
                )
            )
            await published(found, lines)

        monkeypatch.setattr(flusher._publisher, "publish", publish_while_somebody_carries_on_typing)

        await flusher.flush_once()

        assert "first" in github.comments[0][2]
        assert "second" not in github.comments[0][2], "a message the body never carried"
        db_session.expire_all()
        assert await waiting(db_session) == 1, "the message that arrived mid-flush was deleted"


class TestWhoSaidIt:
    async def test_somebody_link_knows_is_named_by_their_github_account(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        registered: Repository,
        clock: list[datetime],
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=registered.discord_guild_id,
            discord_user_id=ALICE,
            github_username="alice-gh",
            github_user_id=1,
        )
        await db_session.commit()
        await say(log, logging_thread, "got the repro")
        clock[0] = AT + QUIET

        await flusher.flush_once()

        assert "[alice-gh](https://github.com/alice-gh)" in github.comments[0][2]

    async def test_somebody_it_does_not_is_named_by_their_discord_name(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        clock: list[datetime],
    ) -> None:
        await say(log, logging_thread, "got the repro")
        clock[0] = AT + QUIET

        await flusher.flush_once()

        body = github.comments[0][2]
        assert "alice" in body
        assert "github.com" not in body


class TestWhenSomethingDies:
    async def test_a_claim_nobody_finished_is_taken_on_and_published_once(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """A process that died after claiming and before writing. The rows are intact, so the
        batch is simply finished by whoever picks it up."""
        await say(log, logging_thread, "one")
        row = await conversation(db_session)
        row.flush_id = "abandoned"
        row.flush_started_at = AT
        row.flush_through_id = 10_000
        await db_session.commit()

        clock[0] = AT + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert len(github.comments) == 1

    async def test_a_claim_still_warm_is_left_alone(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """Otherwise every tick would republish a batch another one is in the middle of."""
        await say(log, logging_thread, "one")
        row = await conversation(db_session)
        row.flush_id = "in flight"
        row.flush_started_at = AT
        row.flush_through_id = 10_000
        await db_session.commit()

        clock[0] = AT + FLUSH_RETRY_AFTER - timedelta(seconds=1)
        await flusher.flush_once()

        assert github.comments == []

    async def test_dying_after_github_accepted_it_publishes_twice(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """The one window this cannot close, pinned here so nobody quietly turns it into silent
        loss instead. Between GitHub accepting the comment and the rows being deleted there is no
        transaction that covers both, and losing part of a transcript is the worse outcome.
        """
        await say(log, logging_thread, "one")
        clock[0] = AT + QUIET
        # Claimed and sent, then the process died before the delete. What survives is exactly
        # this: the claim, the rows, and a comment already live on GitHub.
        row = await conversation(db_session)
        row.flush_id = "sent but never recorded"
        row.flush_started_at = clock[0]
        row.flush_through_id = 10_000
        await db_session.commit()
        await github.add_comment(payloads.OWNER, payloads.REPO, 7, "the first one")

        clock[0] = clock[0] + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert len(github.comments) == 2, "the duplicate is the documented cost, not a regression"

    async def test_everything_in_a_batch_deleted_before_it_went_out(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """Which is the outcome the delete handler exists to produce, so it must not post an
        empty comment or strand the claim."""
        await say(log, logging_thread, "one")
        row = await conversation(db_session)
        row.flush_id = "abandoned"
        row.flush_started_at = AT
        row.flush_through_id = 10_000
        await db_session.commit()
        await log.forget([500])

        clock[0] = AT + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert github.comments == []
        db_session.expire_all()
        assert (await conversation(db_session)).flush_id is None


class TestWhenGitHubSaysNo:
    async def test_the_claim_is_kept_so_the_retry_waits(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """Releasing it would have the next tick, a few seconds later, try the same batch again,
        which during an outage is a write a second at GitHub."""
        await say(log, logging_thread, "one")
        github.write_error = GitHubRateLimitError("spent", retry_after=60)
        clock[0] = AT + QUIET

        await flusher.flush_once()

        db_session.expire_all()
        row = await conversation(db_session)
        assert row.flush_id is not None
        assert row.failed_flushes == 1
        assert await waiting(db_session) == 1, "what was said is still there to try again"

    async def test_it_is_published_once_github_comes_back(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        await say(log, logging_thread, "one")
        github.write_error = GitHubRateLimitError("spent", retry_after=60)
        clock[0] = AT + QUIET
        await flusher.flush_once()

        github.write_error = None
        clock[0] = clock[0] + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert len(github.comments) == 1
        db_session.expire_all()
        assert (await conversation(db_session)).failed_flushes == 0, "the count did not reset"

    async def test_it_is_given_up_on_eventually_and_the_thread_is_told(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """The people whose words were captured are the ones who need to know they were not
        published, and the thread is the only place they will see it."""
        await say(log, logging_thread, "one")
        github.write_error = GitHubRateLimitError("spent", retry_after=60)
        clock[0] = AT + QUIET

        for _ in range(MOST_ATTEMPTS):
            await flusher.flush_once()
            clock[0] = clock[0] + FLUSH_RETRY_AFTER

        said = [what for where, what in threads.posts if where == logging_thread][-1]
        assert "could not be published to GitHub" in said
        db_session.expire_all()
        assert await waiting(db_session) == 0, "the batch was not dropped"

    async def test_logging_carries_on_after_a_batch_is_given_up_on(
        self, log: ConversationLog, logging_thread: int
    ) -> None:
        """The next batch may well work. Giving up on one is not giving up on the thread."""
        assert log.is_logging(logging_thread) is True


class TestWhenTheItemHasGone:
    async def test_the_batch_is_dropped_rather_than_held_for_ever(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """Nothing will ever make it sendable, so holding it would mean retrying for ever."""
        await say(log, logging_thread, "one")
        item = await db_session.scalar(select(LoggedConversation))
        assert item is not None
        await db_session.execute(
            update(LoggedConversation)
            .where(LoggedConversation.id == item.id)
            .values(discord_thread_id=999999)
        )
        await db_session.commit()
        clock[0] = AT + QUIET

        await flusher.flush_once()

        assert github.comments == []
        db_session.expire_all()
        assert await waiting(db_session) == 0


async def test_one_conversation_failing_does_not_take_the_pass_with_it(
    flusher: TranscriptFlusher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    log: ConversationLog,
    logging_thread: int,
    clock: list[datetime],
) -> None:
    """What the poller learned the hard way. A pass that stopped at the first bad conversation
    would leave every one behind it unpublished, and the log would name only the first."""
    await say(log, logging_thread, "one")
    clock[0] = AT + QUIET

    async def explode(*_: object, **__: object) -> None:
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(flusher, "_one", explode)

    with caplog.at_level("ERROR", logger="shannon.services.transcripts.flush"):
        await flusher.flush_once()

    assert "carrying on" in caplog.text


async def test_a_cancelled_pass_is_not_swallowed(
    flusher: TranscriptFlusher,
    monkeypatch: pytest.MonkeyPatch,
    log: ConversationLog,
    logging_thread: int,
    clock: list[datetime],
) -> None:
    """Shutdown cancels the task, and a catch-all that ate it would leave the process waiting out
    its whole grace period on a flusher that had already been told to go."""
    await say(log, logging_thread, "one")
    clock[0] = AT + QUIET

    async def cancelled(*_: object, **__: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(flusher, "_one", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await flusher.flush_once()


class TestTheLoop:
    async def test_it_publishes_until_it_is_asked_to_stop(
        self,
        flusher: TranscriptFlusher,
        log: ConversationLog,
        logging_thread: int,
        github: FakeGitHubClient,
        clock: list[datetime],
    ) -> None:
        await say(log, logging_thread, "one")
        clock[0] = AT + QUIET

        running = asyncio.create_task(flusher.run_forever())
        await until(lambda: bool(github.comments))
        flusher.stop()
        await asyncio.wait_for(running, timeout=5)

        assert len(github.comments) == 1

    async def test_a_stop_wakes_it_rather_than_waiting_out_the_tick(
        self, db_sessionmaker, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        """Shutdown asks rather than cancels, so a tick measured in minutes would hold the whole
        process there. The wait is on an event the stop sets, not on a sleep."""
        slow = TranscriptFlusher(
            db_sessionmaker,
            TranscriptPublisher(github),
            threads,
            quiet_gap=QUIET,
            tick=timedelta(minutes=30),
        )

        running = asyncio.create_task(slow.run_forever())
        await asyncio.sleep(0)
        slow.stop()

        await asyncio.wait_for(running, timeout=5)

    async def test_being_cancelled_ends_it_rather_than_being_logged_and_retried(
        self, flusher: TranscriptFlusher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown asks first and cancels if that is not enough, and a loop that treated the
        cancellation as one more failure to carry on from would never end.

        Written out rather than left to the exception hierarchy. `CancelledError` is a
        `BaseException`, so the catch-all below would not reach it either way; this says the
        intention where somebody reading the loop is looking.
        """

        async def cancelled() -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(flusher, "flush_once", cancelled)

        with pytest.raises(asyncio.CancelledError):
            await flusher.run_forever()

    async def test_a_pass_that_raises_does_not_end_the_loop(
        self,
        flusher: TranscriptFlusher,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A flusher that died would take the feature with it until a restart, and nothing but one
        line in the log would say so."""
        passes = []

        async def explode() -> None:
            passes.append(1)
            raise RuntimeError("the database went away")

        monkeypatch.setattr(flusher, "flush_once", explode)

        with caplog.at_level("ERROR", logger="shannon.services.transcripts.flush"):
            running = asyncio.create_task(flusher.run_forever())
            await until(lambda: len(passes) >= 2)
            flusher.stop()
            await asyncio.wait_for(running, timeout=5)

        assert "carrying on" in caplog.text


class TestClaimsThatCannotBeActedOn:
    async def test_a_half_written_claim_is_left_alone(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        clock: list[datetime],
    ) -> None:
        """The three claim columns are written and cleared together, and nothing in the database
        enforces that. A row holding a flush id and no stamp says nothing about how old it is, so
        it is skipped rather than guessed at."""
        await say(log, logging_thread, "one")
        row = await conversation(db_session)
        row.flush_id = "half written"
        row.flush_started_at = None
        row.flush_through_id = None
        await db_session.commit()

        clock[0] = AT + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert github.comments == []

    async def test_a_claim_somebody_else_took_first_is_left_to_them(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        clock: list[datetime],
    ) -> None:
        """Two passes can both read the same abandoned claim. The seize is guarded on the claim
        still being the one that was read, so only one of them republishes it."""
        await say(log, logging_thread, "one")
        row = await conversation(db_session)
        row.flush_id = "abandoned"
        row.flush_started_at = AT
        row.flush_through_id = 10_000
        await db_session.commit()

        async def taken(*_: object, **__: object) -> bool:
            return False

        monkeypatch.setattr(ConversationStore, "seize", taken)
        clock[0] = AT + FLUSH_RETRY_AFTER
        await flusher.flush_once()

        assert github.comments == []

    async def test_a_batch_emptied_between_reading_it_and_loading_it(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        clock: list[datetime],
    ) -> None:
        """A narrow race rather than an ordinary path: the rows are deleted after the pass has
        listed the conversation and before it reads them. Guarded because the alternative is
        posting a comment with a heading and nothing under it.
        """

        async def nothing(*_: object, **__: object) -> list[object]:
            return []

        monkeypatch.setattr(flusher, "_lines", nothing)
        await say(log, logging_thread, "one")
        clock[0] = AT + QUIET

        await flusher.flush_once()

        assert github.comments == []
        db_session.expire_all()
        assert (await conversation(db_session)).flush_id is None, "the claim was stranded"


async def test_a_thread_that_will_not_take_the_give_up_notice(
    log: ConversationLog,
    flusher: TranscriptFlusher,
    logging_thread: int,
    github: FakeGitHubClient,
    threads: FakeThreadGateway,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    clock: list[datetime],
) -> None:
    """The batch has already been dropped by then, so a thread that refuses the line is not a
    reason to keep retrying a batch that has been given up on."""
    await say(log, logging_thread, "one")
    github.write_error = GitHubRateLimitError("spent", retry_after=60)
    clock[0] = AT + QUIET
    threads.post_error = RuntimeError("Discord said no")

    with caplog.at_level("WARNING", logger="shannon.services.transcripts.flush"):
        for _ in range(MOST_ATTEMPTS):
            await flusher.flush_once()
            clock[0] = clock[0] + FLUSH_RETRY_AFTER

    assert "could not say in thread" in caplog.text
    db_session.expire_all()
    assert await waiting(db_session) == 0


class TestWhoWasTagged:
    """Issue #121, end to end. Tagging somebody in the thread has to reach their GitHub account."""

    async def test_a_tagged_member_link_knows_is_a_live_mention(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        registered: Repository,
        clock: list[datetime],
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=registered.discord_guild_id,
            discord_user_id=BOB,
            github_username="bob-gh",
            github_user_id=2,
        )
        await db_session.commit()
        await say(log, logging_thread, f"hey <@{BOB}> look", mentions={BOB: "Bob"})
        clock[0] = AT + QUIET

        await flusher.flush_once()

        assert "@bob-gh" in github.comments[0][2]

    async def test_a_tagged_member_it_does_not_know_rings_nobody(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        clock: list[datetime],
    ) -> None:
        """The live bug closed coming the other way: a display name that happens to match a login
        used to reach GitHub intact and subscribe a stranger to the item."""
        await say(log, logging_thread, f"hey <@{BOB}> look", mentions={BOB: "torvalds"})
        clock[0] = AT + QUIET

        await flusher.flush_once()

        body = github.comments[0][2]
        assert "torvalds" in body
        assert "@torvalds" not in body

    async def test_what_somebody_typed_is_still_defused_beside_a_live_one(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        registered: Repository,
        clock: list[datetime],
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=registered.discord_guild_id,
            discord_user_id=BOB,
            github_username="bob-gh",
            github_user_id=2,
        )
        await db_session.commit()
        await say(
            log,
            logging_thread,
            f"<@{BOB}> is @octocat upstream?",
            mentions={BOB: "Bob"},
        )
        clock[0] = AT + QUIET

        await flusher.flush_once()

        body = github.comments[0][2]
        assert "@bob-gh" in body
        assert "@octocat" not in body

    async def test_a_link_that_went_away_falls_back_to_the_name(
        self,
        log: ConversationLog,
        flusher: TranscriptFlusher,
        logging_thread: int,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        registered: Repository,
        clock: list[datetime],
    ) -> None:
        """The flush answers from the link table at publish time rather than at capture, so the
        failure direction is towards naming somebody rather than towards ringing the wrong one."""
        await UserLinkStore(db_session).link(
            guild_id=registered.discord_guild_id,
            discord_user_id=BOB,
            github_username="bob-gh",
            github_user_id=2,
        )
        await db_session.commit()
        await say(log, logging_thread, f"hey <@{BOB}>", mentions={BOB: "Bob"})
        await db_session.execute(delete(UserLink).where(UserLink.discord_user_id == BOB))
        await db_session.commit()
        clock[0] = AT + QUIET

        await flusher.flush_once()

        body = github.comments[0][2]
        assert "@bob-gh" not in body
        assert "Bob" in body
