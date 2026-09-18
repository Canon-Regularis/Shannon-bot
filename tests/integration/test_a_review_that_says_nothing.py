"""A review that only wraps inline comments, and the message it no longer posts.

Issue #107. GitHub has no way to leave a note on a diff without submitting a review around it, so
replying once to somebody else's comment submits a `commented` review with no body of its own.
Mirrored, that is `**alice** left a review` with nothing underneath it, once per reply, beside the
comment that actually said something.

What makes this worth its own file is the half that still has to happen. The wrapper is the only
thing that closes the review request it answers, so declining to POST it and declining to READ it
are very different decisions, and the second one pings the reviewer again for the review they just
gave.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote
from shannon.discord_bot.formatting import format_review
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.reviews import is_worth_a_message
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    """A pull request that already has its thread."""
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


def handler_over(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    ran: list[object],
    *,
    declining: bool = True,
):
    """The review path, built by hand so the predicate can be taken away again.

    Built here rather than read off the container, because the one thing every test in this file
    turns on is what changes when the predicate is absent.
    """

    async def then(snapshot: object) -> None:
        ran.append(snapshot)

    mirror = ItemNoteMirror(
        db_sessionmaker,
        threads,
        render=format_review,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        worth_posting=is_worth_a_message if declining else None,
    )
    return build_note_handler(mirror, parse_review_event, then=then)


class TestWhatIsDeclined:
    async def test_a_review_carrying_only_inline_notes_posts_nothing(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        before = len(threads.posts)
        handler = handler_over(db_sessionmaker, threads, [])

        outcome = await handler(
            "submitted", payloads.pull_request_review_event(state="commented", body="")
        )

        assert outcome == "processed"
        assert len(threads.posts) == before

    async def test_a_body_of_nothing_but_whitespace_counts_as_no_body(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Pressing Comment with the summary box holding a stray newline is the same act."""
        before = len(threads.posts)
        handler = handler_over(db_sessionmaker, threads, [])

        await handler(
            "submitted", payloads.pull_request_review_event(state="commented", body="  \n ")
        )

        assert len(threads.posts) == before

    async def test_nothing_is_claimed_for_a_review_that_was_declined(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
    ) -> None:
        """The claim is what a retry reads to decide a note is already in the thread. Taking one
        for a message that was never posted would make undoing this decision later impossible:
        every declined review would read as mirrored and never be posted at all."""
        handler = handler_over(db_sessionmaker, threads, [])

        await handler("submitted", payloads.pull_request_review_event(state="commented", body=""))

        assert await db_session.scalar(select(func.count()).select_from(MirroredNote)) == 0


class TestWhatStillPosts:
    async def test_an_approval_with_no_body_at_all(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The verdict is the content. Approving without writing anything is the common case and
        is exactly what somebody wanted to say."""
        handler = handler_over(db_sessionmaker, threads, [])

        await handler("submitted", payloads.pull_request_review_event(state="approved", body=""))

        assert "approved this pull request" in threads.posts[-1][1]

    async def test_changes_requested_with_no_body(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        handler = handler_over(db_sessionmaker, threads, [])

        await handler(
            "submitted", payloads.pull_request_review_event(state="changes_requested", body="")
        )

        assert "requested changes" in threads.posts[-1][1]

    async def test_a_comment_review_that_actually_says_something(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        handler = handler_over(db_sessionmaker, threads, [])

        await handler(
            "submitted",
            payloads.pull_request_review_event(state="commented", body="two things inline"),
        )

        assert "two things inline" in threads.posts[-1][1]

    async def test_a_mirror_with_no_opinion_posts_everything(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The comments mirror is built without a predicate, and an issue comment carrying no
        text is still a thing somebody did."""
        handler = handler_over(db_sessionmaker, threads, [], declining=False)

        await handler("submitted", payloads.pull_request_review_event(state="commented", body=""))

        assert "left a review" in threads.posts[-1][1]


class TestWhatHappensAnyway:
    async def test_the_review_is_still_read_even_though_it_is_not_posted(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The whole reason this is decided by the mirror and not by the parser. The wrapper is
        what closes the review request it answers, and this is the ordinary way to answer one
        without approving, so a parser that refused it would leave the reviewer pinged again for
        the review they had just given."""
        ran: list[object] = []
        handler = handler_over(db_sessionmaker, threads, ran)

        await handler("submitted", payloads.pull_request_review_event(state="commented", body=""))

        assert len(ran) == 1

    async def test_a_declined_review_on_an_untracked_pull_request_is_still_ignored(
        self,
        tracked: AsyncClient,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A regression test for where this check is allowed to live. Asked before the thread is
        looked up, it would answer `processed` here, and `ignored` is how anybody watching sees
        that a registered repository is sending events for items nobody tracks."""
        handler = handler_over(db_sessionmaker, threads, [])
        payload = payloads.pull_request_review_event(state="commented", body="")
        payload["pull_request"]["number"] = 999

        assert await handler("submitted", payload) == "ignored"
