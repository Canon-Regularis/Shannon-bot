"""The comment this bot publishes does not come back into the thread it was transcribed from.

Issue #103, and the test the whole feature rests on. Every other command in this project writes to
GitHub and RELIES on the delivery coming back, because that echo is what puts the line in the
thread: `services/people.py` says so at length, and `/assign` posting anything itself would put two
of everything in front of a reader. This one is the opposite. The comment was made out of a
conversation that already happened in the thread, so mirroring it back would replay the whole
exchange, under this bot's name, a minute after it was said.

What recognises it is a marker at the front of the body rather than anything about who wrote it.
No App identity to plumb through the webhook path, no network call, no state, and nothing that can
crash between two steps.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import MirroredNote, WebhookEvent
from shannon.domain.enums import DeliveryStatus
from shannon.services.transcripts.lines import MARKER, TranscriptLine, render
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import DeliveryClient, deliver, registered_stack

pytestmark = pytest.mark.integration

AT = datetime(2026, 9, 18, 14, 2, tzinfo=UTC)


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[DeliveryClient]:
    """An issue that already has its thread."""
    async with registered_stack(db_engine, db_session, threads) as http_client:
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        yield http_client


def a_transcript() -> str:
    return render(
        [
            TranscriptLine("alice", AT, "got the repro, it is the label cache"),
            TranscriptLine("bob", AT, "nice, want me to take it?"),
        ]
    )


def posted(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts]


async def notes(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(MirroredNote)) or 0


class TestOurOwnComment:
    async def test_it_is_not_posted_back_into_the_thread(
        self, tracked: DeliveryClient, threads: FakeThreadGateway
    ) -> None:
        """Without this the conversation appears twice: once as people said it, and again as one
        block quoted back at them by the bot."""
        before = len(posted(threads))

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body=a_transcript()),
            delivery="echo1",
        )

        assert len(posted(threads)) == before

    async def test_the_delivery_finishes_rather_than_being_retried(
        self, tracked: DeliveryClient, db_session: AsyncSession
    ) -> None:
        """Declined, not failed. The delivery was understood and acted on; the action was to say
        nothing. Anything else and every transcript this bot posts would be retried sixteen times
        and then sit in the queue as a failure somebody has to look at."""
        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body=a_transcript()),
            delivery="echo2",
        )

        status = await db_session.scalar(
            select(WebhookEvent.status).where(WebhookEvent.github_delivery_id == "echo2")
        )
        assert status == DeliveryStatus.PROCESSED

    async def test_no_claim_is_written_for_it(
        self, tracked: DeliveryClient, db_session: AsyncSession
    ) -> None:
        """The branch that declines a note deliberately takes no claim, so a later decision to
        stop declining these replays every one of them instead of finding them all recorded as
        already mirrored. That property is what makes this reversible."""
        before = await notes(db_session)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body=a_transcript()),
            delivery="echo3",
        )

        assert await notes(db_session) == before


class TestEverybodyElsesComment:
    async def test_an_ordinary_comment_still_reaches_the_thread(
        self, tracked: DeliveryClient, threads: FakeThreadGateway
    ) -> None:
        """The other half, and the one a mistake here breaks silently: suppressing too much would
        stop real comments being mirrored and nothing would say so."""
        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body="I think the cache is the problem"),
            delivery="ordinary1",
        )

        assert any("I think the cache is the problem" in said for said in posted(threads))

    async def test_a_reply_quoting_a_transcript_still_reaches_the_thread(
        self, tracked: DeliveryClient, threads: FakeThreadGateway
    ) -> None:
        """The sharp one. GitHub's quote button copies a body verbatim, marker and all, so a
        suppressor matching anywhere in the body would drop a reply a person actually wrote.
        Anchoring to the start is what stops that: a quote prefixes every line with `> `.
        """
        quoted = "\n".join(f"> {line}" for line in a_transcript().split("\n"))
        reply = f"{quoted}\n\nagreed, taking it"

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body=reply),
            delivery="quotereply1",
        )

        assert any("agreed, taking it" in said for said in posted(threads))

    async def test_a_comment_merely_mentioning_the_marker_is_not_suppressed(
        self, tracked: DeliveryClient, threads: FakeThreadGateway
    ) -> None:
        said = f"why does the bot write {MARKER} at the top of those?"

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event(body=said),
            delivery="mention1",
        )

        assert any("why does the bot write" in body for body in posted(threads))
