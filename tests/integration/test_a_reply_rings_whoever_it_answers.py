"""A reply on a diff rings whoever it answers. Issue #231.

On orthant-kcpc/kcpc-workshops#77 beedware asked for a change on a line, mkutay answered "done",
and beedware was never told: a reply's webhook names the comment it was left under and nothing
about who wrote it, and the mirror rang nobody but the names typed in the answer. The thread is
read from GitHub now, between finding where the note goes and resolving who it may ring.

Where that read sits is most of what can go wrong with it, so the first class drives the mirror
with a stand-in for it and watches only the position: no connection held across it, nothing read
for an item nobody tracks, nothing claimed before it, and an edit read for as well.

The rest go through the stack the container builds, with GitHub stocked with the thread, so the
wiring is under test too - and through the real worker where what matters is what a failure does
to the delivery: a blip holds a young reply back until GitHub answers, and nothing else does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from shannon.db.models import MirroredNote
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.formatting import format_review_comment
from shannon.domain.models import Actor, ItemNote, ReviewCommentSnapshot
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.webhooks.events import EventHandler
from shannon.github.webhooks.review_comments import parse_review_comment_event
from shannon.services.notes import Answering, ItemNoteMirror, build_note_handler
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

# The thread from the issue, with its real comment ids and two made-up Discord accounts.
OPENER_ID = 4197046428
REPLY_ID = 4197154572
ACCOUNTS = {"beedware": 3001, "mkutay": 4001}
BEEDWARE = 3030
MKUTAY = 4040


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    """A pull request that already has its thread."""
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


async def link(session: AsyncSession, login: str, discord_user_id: int) -> None:
    """What /link leaves behind, with the GitHub id the item's payloads carry for that login."""
    await UserLinkStore(session).link(
        guild_id=1,
        github_username=login,
        github_user_id=ACCOUNTS[login],
        discord_user_id=discord_user_id,
    )
    await session.commit()


def person(login: str) -> Actor:
    return Actor(login, ACCOUNTS[login])


def reply(action: str = "created", **overrides: Any) -> dict[str, Any]:
    """mkutay's "done", left under beedware's comment."""
    fields: dict[str, Any] = {
        "id": REPLY_ID,
        "in_reply_to_id": OPENER_ID,
        "user": payloads.user("mkutay", ACCOUNTS["mkutay"]),
        "body": "done",
        "html_url": f"https://github.com/{payloads.OWNER}/{payloads.REPO}/pull/7"
        f"#discussion_r{REPLY_ID}",
    }
    fields.update(overrides)
    return payloads.pull_request_review_comment_event(action, **fields)


def last_post(threads: FakeThreadGateway) -> tuple[str, object]:
    """The message the last delivery posted, refusing to read anything else as one."""
    kind, _, content, notify = threads.allowed[-1]
    assert kind == "post", f"the last write was a {kind}, so nothing was posted"
    return content, notify


class Answers:
    """A stand-in for the read, answering with whoever it is told and remembering each ask."""

    def __init__(self, *people: Actor, failing: Exception | None = None) -> None:
        self.people = people
        self.failing = failing
        self.asked: list[ItemNote] = []

    async def __call__(self, note: ItemNote) -> ItemNote:
        self.asked.append(note)
        if self.failing is not None:
            raise self.failing
        assert isinstance(note, ReviewCommentSnapshot)
        return replace(note, replying_to=self.people)


def handler_with(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    answering: Answering,
) -> EventHandler:
    """The inline-comment path, built by hand around whatever stands in for the read."""
    mirror = ItemNoteMirror(
        db_sessionmaker,
        threads,
        render=format_review_comment,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        answering=answering,
    )
    return build_note_handler(mirror, parse_review_comment_event)


class TestTheReadBeforeThePost:
    async def test_whoever_the_read_finds_is_named_and_rung(
        self,
        tracked: DeliveryClient,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Resolved off the same map as everybody else, so the mention in the heading and the
        allow-list beside it come from one place. mkutay is named and not rung: he wrote it."""
        await link(db_session, "beedware", BEEDWARE)
        await link(db_session, "mkutay", MKUTAY)
        handle = handler_with(db_sessionmaker, threads, Answers(person("beedware")))

        assert await handle("created", reply()) == "processed"

        content, notify = last_post(threads)
        assert content.startswith(f"**<@{MKUTAY}>** replied to <@{BEEDWARE}> on")
        assert notify == (BEEDWARE,)

    async def test_no_connection_is_held_while_github_is_read(
        self,
        tracked: DeliveryClient,
        db_engine: AsyncEngine,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A read from GitHub can take seconds, and a pool held open across it is a pool the
        deliveries behind this one wait on."""
        pool = db_engine.pool
        assert isinstance(pool, QueuePool)
        baseline = pool.checkedout()
        held: list[int] = []

        async def answering(note: ItemNote) -> ItemNote:
            held.append(pool.checkedout())
            return note

        await handler_with(db_sessionmaker, threads, answering)("created", reply())

        assert held == [baseline], "a connection was checked out while GitHub was being read"

    async def test_a_note_on_an_item_nobody_tracks_is_never_read_for(
        self,
        tracked: DeliveryClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A note on something nobody is watching costs no GitHub calls at all."""
        answers = Answers(person("beedware"))
        payload = reply()
        payload["pull_request"]["number"] = 999

        assert (
            await handler_with(db_sessionmaker, threads, answers)("created", payload) == "ignored"
        )
        assert answers.asked == []

    async def test_a_read_that_fails_claims_nothing_and_the_retry_posts_once(
        self,
        tracked: DeliveryClient,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Before the claim, so a failure sends the delivery round again with nothing taken.
        After it, the retry would find the reply recorded as posted and it would be lost."""
        answers = Answers(person("beedware"), failing=GitHubUnavailableError("GitHub is down"))
        handle = handler_with(db_sessionmaker, threads, answers)
        before = len(threads.posts)

        with pytest.raises(GitHubUnavailableError):
            await handle("created", reply())

        assert len(threads.posts) == before
        claimed = await db_session.scalar(
            select(func.count())
            .select_from(MirroredNote)
            .where(MirroredNote.note_key == f"review-comment:{REPLY_ID}")
        )
        assert claimed == 0

        answers.failing = None
        await handle("created", reply())

        assert len(threads.posts) == before + 1
        assert "replied to beedware on" in threads.posts[-1][1]

    async def test_an_edit_is_read_for_too_and_rings_nobody(
        self,
        tracked: DeliveryClient,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The heading is drawn again for an edit, and would stop saying whom it answered."""
        await link(db_session, "beedware", BEEDWARE)
        answers = Answers(person("beedware"))
        handle = handler_with(db_sessionmaker, threads, answers)
        await handle("created", reply())
        rung = len(threads.allowed)

        await handle("edited", reply("edited", body="done, and the slides too"))

        assert len(answers.asked) == 2
        assert len(threads.allowed) == rung, "the edit was posted, or rang somebody"
        assert f"replied to <@{BEEDWARE}> on" in threads.revisions[-1][2]
        assert "and the slides too" in threads.revisions[-1][2]


REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()


@pytest.fixture
def github() -> FakeGitHubClient:
    """GitHub as a reply finds it: whatever inline comments a test stocks, and nothing else."""
    return FakeGitHubClient()


@pytest_asyncio.fixture
async def on_github(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
) -> AsyncIterator[DeliveryClient]:
    """The whole stack as the container wires it, over a pull request that has its thread."""
    async with registered_stack(db_engine, db_session, threads, github=github) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


def opener(**overrides: Any) -> dict[str, Any]:
    """beedware's comment, the one the thread on #77 opened with."""
    fields: dict[str, Any] = {
        "id": OPENER_ID,
        "user": payloads.user("beedware", ACCOUNTS["beedware"]),
        "body": "Elongate the river, its a bit small, do the same for the slides",
    }
    fields.update(overrides)
    return payloads.pull_request_review_comment_event("created", **fields)


def stock(github: FakeGitHubClient, *comments: dict[str, Any]) -> None:
    """What GitHub lists for the pull request, read through the parser the webhook goes through,
    so a comment GitHub lists and one it delivers cannot describe different things."""
    listed: list[ReviewCommentSnapshot] = []
    for payload in comments:
        snapshot = parse_review_comment_event("created", payload)
        assert isinstance(snapshot, ReviewCommentSnapshot)
        listed.append(snapshot)
    github.review_comments[(REPO_FULL, 7)] = listed


def young(**overrides: Any) -> dict[str, Any]:
    """The reply, written a moment ago. The payload helper dates everything in August, which is
    long past the wait for a GitHub that is down."""
    return reply(created_at=datetime.now(UTC).isoformat(), **overrides)


class TestTheReplyTheIssueReports:
    async def test_the_person_a_reply_answers_is_rung(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """#77 exactly: beedware is told mkutay answered him, and mkutay is not told he did."""
        await link(db_session, "beedware", BEEDWARE)
        await link(db_session, "mkutay", MKUTAY)
        stock(github, opener(), reply())

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        content, notify = last_post(threads)
        assert content.startswith(f"**<@{MKUTAY}>** replied to <@{BEEDWARE}> on")
        assert notify == (BEEDWARE,)

    async def test_somebody_unlinked_is_named_and_rung_by_nobody(
        self, on_github: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        stock(github, opener(), reply())

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        content, notify = last_post(threads)
        assert content.startswith("**mkutay** replied to beedware on")
        assert notify == ()

    async def test_answering_the_answer_rings_the_other_side(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """beedware thanking mkutay for "done". The thread's opener is the person replying now,
        so a rule that rang only the opener would ring nobody at all."""
        await link(db_session, "beedware", BEEDWARE)
        await link(db_session, "mkutay", MKUTAY)
        thanks = reply(
            id=REPLY_ID + 1000, user=payloads.user("beedware", ACCOUNTS["beedware"]), body="ty"
        )
        stock(github, opener(), reply(), thanks)

        await deliver(on_github, "pull_request_review_comment", thanks, delivery="rc2")

        content, notify = last_post(threads)
        assert content.startswith(f"**<@{BEEDWARE}>** replied to <@{MKUTAY}> on")
        assert notify == (MKUTAY,)

    async def test_a_reply_in_a_thread_of_your_own_answers_nobody(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        await link(db_session, "mkutay", MKUTAY)
        stock(github, opener(user=payloads.user("mkutay", ACCOUNTS["mkutay"])), reply())

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        content, notify = last_post(threads)
        assert content.startswith(f"**<@{MKUTAY}>** replied on")
        assert notify == ()

    async def test_a_muted_member_it_answers_is_named_and_not_rung(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        await link(db_session, "beedware", BEEDWARE)
        await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=BEEDWARE)
        await db_session.commit()
        stock(github, opener(), reply())

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        content, notify = last_post(threads)
        assert f"replied to <@{BEEDWARE}> on" in content
        assert notify == ()

    async def test_a_link_that_changed_hands_is_not_rung_even_where_the_reply_names_them(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """The order names are resolved in, pinned. GitHub's id for whoever a reply answers is
        laid over the bare name the body writes, so a link held for an account that is no longer
        that login is caught by it. The other way round, the body's name would resolve on the
        link alone and ring whoever holds it now."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="beedware", github_user_id=9999, discord_user_id=BEEDWARE
        )
        await db_session.commit()
        stock(github, opener(), reply())

        await deliver(
            on_github,
            "pull_request_review_comment",
            reply(body="@beedware done"),
            delivery="rc1",
        )

        content, notify = last_post(threads)
        assert f"<@{BEEDWARE}>" not in content
        assert content.startswith("**mkutay** replied to beedware on")
        assert notify == ()

    async def test_a_comment_that_opens_a_thread_asks_github_nothing(
        self, on_github: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(on_github, "pull_request_review_comment", opener(), delivery="rc1")

        assert "commented on" in last_post(threads)[0]
        assert github.review_comment_calls == []

    async def test_a_reply_on_a_pull_request_nobody_tracks_asks_github_nothing(
        self, on_github: DeliveryClient, github: FakeGitHubClient
    ) -> None:
        payload = reply()
        payload["pull_request"]["number"] = 999

        await deliver(on_github, "pull_request_review_comment", payload, delivery="rc1")

        assert await on_github.outcome_of("rc1") == "ignored"
        assert github.review_comment_calls == []

    async def test_an_edit_keeps_whom_it_answered_and_rings_nobody(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        await link(db_session, "beedware", BEEDWARE)
        stock(github, opener(), reply())
        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")
        rung = len(threads.allowed)

        await deliver(
            on_github,
            "pull_request_review_comment",
            reply("edited", body="done, and the slides too"),
            delivery="rc2",
        )

        assert len(threads.allowed) == rung, "the edit was posted, or rang somebody"
        assert f"replied to <@{BEEDWARE}> on" in threads.revisions[-1][2]
        assert len(github.review_comment_calls) == 2

    async def test_a_reply_under_a_comment_github_does_not_list_says_it_is_a_reply(
        self, on_github: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        stock(github, reply())

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        assert last_post(threads)[0].startswith("**mkutay** replied on")


class TestWhenGitHubWillNotSay:
    """Through the real worker, because what a failure does to the delivery is the point."""

    async def test_a_pull_request_github_no_longer_has_posts_the_reply_naming_nobody(
        self, on_github: DeliveryClient, github: FakeGitHubClient, threads: FakeThreadGateway
    ) -> None:
        github.review_comments[(REPO_FULL, 7)] = None

        await deliver(on_github, "pull_request_review_comment", young(), delivery="rc1")

        assert await on_github.outcome_of("rc1") == "processed"
        assert last_post(threads)[0].startswith("**mkutay** replied on")

    @pytest.mark.parametrize(
        "error",
        [
            GitHubUnavailableError("GitHub is down"),
            GitHubRateLimitError("slow down", retry_after=60),
        ],
    )
    async def test_a_young_reply_waits_out_a_blip_and_rings_them_once_github_answers(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        error: GitHubError,
    ) -> None:
        await link(db_session, "beedware", BEEDWARE)
        stock(github, opener(), reply())
        github.review_comment_error = error
        before = len(threads.posts)

        await deliver(
            on_github,
            "pull_request_review_comment",
            young(),
            delivery="rc1",
            expect_retries=True,
        )

        assert await on_github.outcome_of("rc1") == "pending"
        assert await on_github.attempts_of("rc1") == 1
        assert len(threads.posts) == before, "the reply went out without waiting"

        github.review_comment_error = None
        await on_github.drain()

        assert len(threads.posts) == before + 1
        content, notify = last_post(threads)
        assert f"replied to <@{BEEDWARE}> on" in content
        assert notify == (BEEDWARE,)

    async def test_an_old_reply_is_posted_without_them_rather_than_held(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Dated in August by the payload helper, so long past the wait."""
        await link(db_session, "beedware", BEEDWARE)
        stock(github, opener(), reply())
        github.review_comment_error = GitHubUnavailableError("GitHub is down")

        await deliver(on_github, "pull_request_review_comment", reply(), delivery="rc1")

        assert await on_github.outcome_of("rc1") == "processed"
        content, notify = last_post(threads)
        assert content.startswith("**mkutay** replied on")
        assert notify == ()

    @pytest.mark.parametrize("error", [GitHubAuthError("no token"), GitHubRefusedError("will not")])
    async def test_a_refusal_posts_even_a_young_reply_at_once_without_them(
        self,
        on_github: DeliveryClient,
        github: FakeGitHubClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        error: GitHubError,
    ) -> None:
        await link(db_session, "beedware", BEEDWARE)
        stock(github, opener(), reply())
        github.review_comment_error = error

        await deliver(on_github, "pull_request_review_comment", young(), delivery="rc1")

        assert await on_github.outcome_of("rc1") == "processed"
        # Asked once and never parked: a failed attempt is the only thing that counts as one.
        assert len(github.review_comment_calls) == 1
        assert await on_github.attempts_of("rc1") == 0
        content, notify = last_post(threads)
        assert content.startswith("**mkutay** replied on")
        assert notify == ()
