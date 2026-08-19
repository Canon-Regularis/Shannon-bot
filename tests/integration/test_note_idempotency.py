"""A note goes into its thread once, however many times its delivery is handled.

The queue is at-least-once on purpose. A delivery whose status could not be written stays
leased, comes back when the lease runs out, and is handled again from the top. Every other
handler survives that: syncing an item upserts its row, swaps the thread pointer from the id it
read, and claims a ping before sending it. Mirroring a note had none of that and simply posted
again, so a dropped connection at the wrong moment put the same comment in the thread twice.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import MirroredNote, Repository, WebhookEvent
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack

pytestmark = pytest.mark.integration


def a_comment(**overrides):
    return payloads.issue_comment_event("created", on=payloads.pull_request_as_issue(), **overrides)


async def expire_the_lease(container, delivery: str) -> None:
    """Put the delivery back where a dead worker's rows end up: leased, but not for much longer."""
    async with container.sessionmaker() as session, session.begin():
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.github_delivery_id == delivery)
            .values(locked_until=text("now() - interval '1 hour'"))
        )


def comment_posts(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if "commented" in body]


async def with_a_thread(client, container) -> None:
    await post(client, "pull_request", payloads.pull_request_event("opened"), delivery="pr-1")
    await container.worker.run_once()


async def test_a_delivery_handled_twice_posts_the_comment_once(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The handler succeeds and the write that records it does not, which is the whole gap."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        await post(client, "issue_comment", a_comment(), delivery="comment-1")

        real_finish, calls = container.queue.finish, {"n": 0}

        async def drops_the_first_write(delivery, status):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the connection went away before the status landed")
            return await real_finish(delivery, status)

        container.queue.finish = drops_the_first_write
        with pytest.raises(RuntimeError):
            # run_forever swallows this and carries on; run_once is where it surfaces.
            await container.worker.run_once()

        await expire_the_lease(container, "comment-1")
        await container.worker.run_once()

    assert len(comment_posts(threads)) == 1, "the comment was mirrored more than once"


async def test_a_post_that_fails_is_still_owed_and_arrives_on_the_retry(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Claiming before posting only works if a failed post gives the claim back.

    Without that, the first attempt records the note as posted, the post fails, and every retry
    reads it as already done. The note is then lost with nothing saying so, which is worse than
    the duplicate this claiming exists to prevent.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        # The stand-in goes in after the thread exists, or it breaks the item's own metadata
        # post instead of the note's, which is a different test entirely.
        await with_a_thread(client, container)
        refusals = {"left": 1}
        real_post = threads.post

        async def refuses_once(**kwargs):
            if refusals["left"]:
                refusals["left"] -= 1
                raise DiscordGatewayError("Discord refused to post")
            return await real_post(**kwargs)

        threads.post = refuses_once
        await post(client, "issue_comment", a_comment(), delivery="comment-1")

        await container.worker.run_once()
        assert comment_posts(threads) == []
        held = await db_session.scalar(select(func.count()).select_from(MirroredNote))
        assert held == 0, "a note that never reached the thread is still recorded as posted"

        async with container.sessionmaker() as session, session.begin():
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.github_delivery_id == "comment-1")
                .values(next_attempt_at=None)
            )
        await container.worker.run_once()

    assert len(comment_posts(threads)) == 1, "the note never arrived after its retry"


async def test_a_thread_that_was_deleted_gives_the_claim_back_too(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """This path lets go of the dead thread and asks to be retried, so it owes the claim back."""
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    async with client:
        await with_a_thread(client, container)
        gone = {"still": True}
        real_post = threads.post

        async def thread_is_gone(**kwargs):
            if gone["still"]:
                gone["still"] = False
                raise ThreadNotFoundError("somebody deleted the thread")
            return await real_post(**kwargs)

        threads.post = thread_is_gone
        await post(client, "issue_comment", a_comment(), delivery="comment-1")
        await container.worker.run_once()

        held = await db_session.scalar(select(func.count()).select_from(MirroredNote))

    assert held == 0, "the note was recorded as posted into a thread that had been deleted"


async def test_a_comment_and_a_review_sharing_a_number_are_different_notes(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """GitHub numbers comments and reviews separately, so the two collide.

    Keyed on the number alone, whichever arrived second would be taken for one already posted
    and silently dropped.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    client = build_http_client(container)

    shared = 4242
    async with client:
        await with_a_thread(client, container)
        await post(client, "issue_comment", a_comment(id=shared), delivery="comment-1")
        await post(
            client,
            "pull_request_review",
            payloads.pull_request_review_event("submitted", id=shared),
            delivery="review-1",
        )
        while await container.worker.run_once():
            pass

    keys = set((await db_session.scalars(select(MirroredNote.note_key))).all())
    assert keys == {f"comment:{shared}", f"review:{shared}"}
    assert len(threads.posts) >= 2, "one of the two notes was dropped as a duplicate of the other"
