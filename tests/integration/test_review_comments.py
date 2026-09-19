"""Inline review comments, from the delivery to the thread.

Issue #107. A review's summary already reached Discord; what it says about the code did not, and
that is where a review actually lives. One message per comment, because GitHub delivers the review
and its comments separately with no promised order, so anything that gathered them up first would
be waiting on a delivery that might never come.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import ItemAssignment, MirroredNote, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import ThreadNotFoundError
from shannon.domain.enums import ActorRole
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.signing import post
from tests.support.stack import build_http_client, build_stack, deliver, registered_stack

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()


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


def inline_posts(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts if "commented on" in body or "replied on" in body]


class TestWhatReachesTheThread:
    async def test_an_inline_comment_lands_in_the_pull_request_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        response = await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(),
            delivery="rc1",
        )

        assert response.json()["status"] == "accepted"
        assert len(inline_posts(threads)) == 1

    async def test_it_says_which_file_and_line(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(),
            delivery="rc1",
        )

        said = inline_posts(threads)[-1]
        assert "**monalisa** commented on `shannon/services/notes.py` L205" in said
        assert "This claim wants giving back on cancellation too." in said
        assert said.splitlines()[-1].endswith(f"#discussion_r{payloads.REVIEW_COMMENT_ID}>")

    async def test_a_reply_is_told_apart_from_a_first_comment(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(in_reply_to_id=98765),
            delivery="rc1",
        )

        assert "replied on" in inline_posts(threads)[-1]

    async def test_every_comment_in_one_round_gets_its_own_message(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """One delivery each, so nothing here has to hold a batch together or cap one."""
        for number, path in enumerate(("shannon/container.py", "shannon/services/notes.py")):
            await deliver(
                tracked,
                "pull_request_review_comment",
                payloads.pull_request_review_comment_event(id=900 + number, path=path),
                delivery=f"rc{number}",
            )

        said = inline_posts(threads)
        assert len(said) == 2
        assert "`shannon/container.py`" in said[0]
        assert "`shannon/services/notes.py`" in said[1]

    async def test_a_name_in_the_body_reaches_the_person_it_names(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """An inline comment is where somebody is most likely to be asked something directly."""
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=606
        )
        await db_session.commit()

        await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(body="@hubot is this still needed?"),
            delivery="rc1",
        )

        assert "<@606>" in inline_posts(threads)[-1]


class TestWhatItLeavesAlone:
    async def test_an_edited_comment_is_not_mirrored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """The thread records what was said when it was said, which is the rule every other note
        on this path already follows."""
        before = len(threads.posts)

        response = await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event("edited"),
            delivery="rc1",
        )

        assert response.json()["status"] == "ignored"
        assert len(threads.posts) == before

    async def test_a_comment_on_an_untracked_pull_request_is_ignored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)
        payload = payloads.pull_request_review_comment_event()
        payload["pull_request"]["number"] = 999

        await deliver(tracked, "pull_request_review_comment", payload, delivery="rc1")

        assert await tracked.outcome_of("rc1") == "ignored"
        assert len(threads.posts) == before

    async def test_a_comment_on_a_closed_pull_request_leaves_its_thread_shut(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """Reviews carry on landing after a pull request closes, and posting is what wakes an
        archived thread. Without putting it back, every late note drags a finished item's thread
        into the channel and leaves it there."""
        await deliver(
            tracked,
            "pull_request",
            payloads.pull_request_event("closed", state="closed", merged=True),
            delivery="p1",
        )
        thread_id = threads.created[0].thread_id
        assert threads.threads[thread_id].archived is True

        await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(),
            delivery="rc1",
        )

        assert len(inline_posts(threads)) == 1
        assert threads.unarchived[-1] == thread_id, "the comment never reopened the thread to land"
        assert threads.threads[thread_id].archived is True, "the comment left the thread open"


class TestTheWholeReviewRound:
    async def test_the_comments_land_and_the_wrapper_around_them_does_not(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """What somebody actually sees after one round of review: what was said about the code,
        and not a line announcing that saying it counted as a review."""
        for number in (0, 1):
            await deliver(
                tracked,
                "pull_request_review_comment",
                payloads.pull_request_review_comment_event(id=900 + number),
                delivery=f"rc{number}",
            )
        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(state="commented", body=""),
            delivery="r1",
        )

        assert len(inline_posts(threads)) == 2
        assert not any("left a review" in body for _, body in threads.posts)

    async def test_the_wrapper_still_closes_the_review_request_it_answers(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """The reason this is decided by the mirror rather than by the parser, proved end to end.

        Refusing the wrapper one step earlier would leave this column null, and the next ordinary
        delivery would ping the reviewer to review what they had just reviewed.
        """
        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(state="commented", body=""),
            delivery="r1",
        )

        fulfilled = await db_session.scalar(
            select(ItemAssignment.fulfilled_at).where(
                ItemAssignment.role_type == ActorRole.REVIEWER,
                ItemAssignment.github_username == "monalisa",
            )
        )
        assert fulfilled is not None, "the reviewer will be asked again for the review they gave"


class TestTheSameCommentTwice:
    async def test_a_repeated_delivery_posts_it_once(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        payload = payloads.pull_request_review_comment_event()

        first = await deliver(tracked, "pull_request_review_comment", payload, delivery="same")
        second = await deliver(tracked, "pull_request_review_comment", payload, delivery="same")

        assert (first.json()["status"], second.json()["status"]) == ("accepted", "duplicate")
        assert len(inline_posts(threads)) == 1

    async def test_a_redelivery_under_a_new_id_still_posts_it_once(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        payload = payloads.pull_request_review_comment_event()

        await deliver(tracked, "pull_request_review_comment", payload, delivery="one")
        await deliver(tracked, "pull_request_review_comment", payload, delivery="two")

        assert len(inline_posts(threads)) == 1

    async def test_it_keys_on_a_space_of_its_own(
        self, tracked: AsyncClient, db_session: AsyncSession
    ) -> None:
        await deliver(
            tracked,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(id=4242),
            delivery="rc1",
        )

        keys = set((await db_session.scalars(select(MirroredNote.note_key))).all())
        assert "review-comment:4242" in keys


async def test_a_comment_on_a_deleted_thread_rebuilds_the_pull_request_and_not_an_issue(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession, pr_event
) -> None:
    """Which kind of item it is, is fixed on the snapshot rather than read off the body.

    The rebuild that mends a deleted thread branches on that field, and its other arm reads the
    item through the issues endpoint. GitHub serves a pull request there quite happily, so a
    snapshot claiming to be an issue would not fail: it would upsert a second tracked item under
    the issue's own id and open a duplicate thread in the issues channel.
    """
    threads = FakeThreadGateway()
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): pr_event("opened")})
    container = build_stack(db_engine, threads=threads, github=github)
    client = build_http_client(container)

    async with client:
        await post(client, "pull_request", payloads.pull_request_event("opened"), delivery="pr-1")
        await container.worker.run_once()
        first_thread = threads.created[0].thread_id
        real_post = threads.post

        async def the_thread_is_gone(**kwargs):
            if kwargs["thread_id"] == first_thread:
                raise ThreadNotFoundError("somebody deleted the thread")
            return await real_post(**kwargs)

        threads.post = the_thread_is_gone
        await post(
            client,
            "pull_request_review_comment",
            payloads.pull_request_review_comment_event(),
            delivery="rc-1",
        )
        await container.worker.run_once()

        assert len(threads.created) == 2, "nothing rebuilt the thread the comment could not reach"
        assert threads.created[1].channel_id == 99, "it was rebuilt as an issue, not a pull request"

        async with container.sessionmaker() as session:
            tracked_items = await session.scalar(select(func.count()).select_from(TrackedItem))
        assert tracked_items == 1, "the rebuild opened a second item for the same pull request"
