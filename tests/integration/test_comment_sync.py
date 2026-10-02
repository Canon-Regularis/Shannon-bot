from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote, Repository, TrackedItem
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.panels import Panel
from shannon.domain.enums import ObjectType, Status
from shannon.github.webhooks.comments import parse_comment_event
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import IssuePolicy
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.stack import deliver, registered_stack

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[AsyncClient]:
    """An issue and a pull request, both already synced.

    GitHub knows the pull request, which it did not before and had to. An approving review posts
    its note and then asks whether everybody has approved, which READS the pull request - and a
    fake that has never heard of it answered 404, so the delivery was parked five seconds out and
    never finished. Every test here about an approval was asserting on a note that had landed and
    a delivery that had failed, and the two negative ones - no metadata touched, no status moved -
    were passing because nothing had happened at all.
    """
    github = FakeGitHubClient(pull_requests={(f"{payloads.OWNER}/{payloads.REPO}".lower(), 7): PR})
    async with registered_stack(db_engine, db_session, threads, github=github) as http_client:
        await deliver(http_client, "issues", payloads.issue_event("opened"), delivery="i0")
        await deliver(
            http_client, "pull_request", payloads.pull_request_event("opened"), delivery="p0"
        )
        yield http_client


# The pull request as GitHub would answer for it, parsed from the same payload the webhook
# carries so the two cannot describe different things.
PR = parse_pull_request_event("opened", payloads.pull_request_event("opened"))


def thread_for(threads: FakeThreadGateway, channel_id: int) -> int:
    return next(t.thread_id for t in threads.created if t.channel_id == channel_id)


def note_message(threads: FakeThreadGateway, thread_id: int) -> int:
    """The message a note was posted as: the one that is not the thread's metadata block."""
    thread = threads.threads[thread_id]
    return next(mid for mid in thread.messages if mid != thread.metadata_message_id)


async def test_a_comment_on_an_issue_reaches_its_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    response = await deliver(
        tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1"
    )

    assert response.json()["status"] == "accepted"
    posted = [
        content for thread_id, content in threads.posts if thread_id == thread_for(threads, 98)
    ]
    assert any("monalisa" in content for content in posted)


async def test_the_comment_carries_everything_the_issue_asks_for(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    content = threads.posts[-1][1]
    assert "**monalisa** commented" in content
    assert "<t:" in content
    assert "Reproduced on main" in content
    assert f"issuecomment-{payloads.COMMENT_ID}" in content


async def test_a_comment_on_a_pull_request_reaches_the_pull_request_thread(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    """The comment payload reports the issue id, which never matches the stored pull request id.

    Matching on number is the only reason this lands anywhere.
    """
    payload = payloads.issue_comment_event(on=payloads.pull_request_as_issue())

    response = await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert response.json()["status"] == "accepted"
    assert threads.posts[-1][0] == thread_for(threads, 99)


async def test_a_linked_commenter_is_mentioned(
    tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
) -> None:
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=909
    )
    await db_session.commit()

    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    assert "<@909>" in threads.posts[-1][1]


async def test_a_comment_on_an_untracked_item_is_ignored(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event(on=payloads.issue(id=999, number=999))

    await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert await tracked.outcome_of("c1") == "ignored"
    assert len(threads.posts) == before


async def test_a_comment_from_another_repository_is_ignored(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event()
    payload["repository"]["id"] = 999999

    await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert await tracked.outcome_of("c1") == "ignored"
    assert len(threads.posts) == before


async def test_comments_never_duplicate_the_metadata_message(
    tracked: AsyncClient, threads: FakeThreadGateway, db_session: AsyncSession
) -> None:
    thread_id = thread_for(threads, 98)
    metadata_before = threads.metadata_of(thread_id)
    message_before = await db_session.scalar(
        select(TrackedItem.discord_message_id).where(
            TrackedItem.github_object_type == ObjectType.ISSUE
        )
    )

    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    db_session.expunge_all()
    message_after = await db_session.scalar(
        select(TrackedItem.discord_message_id).where(
            TrackedItem.github_object_type == ObjectType.ISSUE
        )
    )
    assert message_after == message_before
    assert threads.metadata_of(thread_id) == metadata_before
    assert len(threads.created) == 2


async def test_a_repeated_comment_delivery_posts_once(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    before = len(threads.posts)
    payload = payloads.issue_comment_event()

    first = await deliver(tracked, "issue_comment", payload, delivery="c1")
    second = await deliver(tracked, "issue_comment", payload, delivery="c1")

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert len(threads.posts) == before + 1


class TestAnEditedCommentIsShown:
    """Issue #165. A comment edited on GitHub used to leave Discord showing text that existed
    nowhere any more, because the delivery was turned away at the gate.

    Rewritten in place rather than posted again underneath. A second message would say the same
    thing twice and would ring everybody the comment names for a typo somebody fixed; an edit says
    it once and rings nobody, because Discord sends no notification for an edit whatever it says.
    """

    async def mirrored(self, tracked: AsyncClient) -> None:
        await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    async def test_the_edit_rewrites_the_message_rather_than_posting_another(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """The whole of the issue, in two numbers: one more rewrite, not one more message."""
        await self.mirrored(tracked)
        thread_id = thread_for(threads, 98)
        message_id = note_message(threads, thread_id)
        before = len(threads.posts)

        response = await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event("edited", body="Reproduced on main, and on 1.2 as well"),
            delivery="c2",
        )

        assert response.json()["status"] == "accepted"
        assert len(threads.posts) == before, "the edit was posted as a second message"
        assert threads.revisions[-1][:2] == (thread_id, message_id)
        assert "and on 1.2 as well" in threads.revisions[-1][2]

    async def test_the_thread_keeps_the_new_text_and_loses_the_old(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """What a reader of the thread actually sees afterwards, read off the message itself
        rather than off the call that wrote it."""
        await self.mirrored(tracked)
        thread_id = thread_for(threads, 98)
        message_id = note_message(threads, thread_id)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event("edited", body="Actually it was the cache"),
            delivery="c2",
        )

        shown = threads.threads[thread_id].messages[message_id]
        assert "Actually it was the cache" in shown
        assert "Reproduced on main" not in shown, "the thread still shows the text that was edited"

    async def test_a_name_the_edit_adds_becomes_a_real_discord_mention(
        self,
        tracked: AsyncClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """The half of #165 that is about people rather than text.

        Somebody added to a comment by an edit shows up as the tagged individual, exactly as they
        would had the comment named them from the start - the renderer is handed the same map
        either way, built from the body THIS delivery carries.
        """
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="whaletheree", github_user_id=404, discord_user_id=707
        )
        await db_session.commit()
        await self.mirrored(tracked)
        assert "<@707>" not in threads.posts[-1][1], "the original already named them"

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event("edited", body="cc @whaletheree on this"),
            delivery="c2",
        )

        assert "<@707>" in threads.revisions[-1][2]

    async def test_the_edit_rings_nobody(
        self,
        tracked: AsyncClient,
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Nobody already named is rung again for a typo, and that is not a decision this code
        gets to make: Discord notifies nobody for an edit, so `revise` is not even offered an
        allow-list to spend. Watching `allowed` is how that shows from outside.
        """
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=909
        )
        await db_session.commit()
        await self.mirrored(tracked)
        spent = len(threads.allowed)

        await deliver(
            tracked,
            "issue_comment",
            payloads.issue_comment_event("edited", body="cc @monalisa again"),
            delivery="c2",
        )

        assert threads.revisions, "nothing was rewritten, so this proves nothing"
        assert len(threads.allowed) == spent, "the edit spent an allow-list, so it rang somebody"

    async def test_an_edit_of_a_comment_never_mirrored_posts_it(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """No create first. The claim is what tells the two cases apart, so an edit that finds no
        claim taken has nothing to rewrite and posts instead - which repairs a comment whose own
        delivery never arrived rather than dropping it for good.
        """
        before = len(threads.posts)

        response = await deliver(
            tracked, "issue_comment", payloads.issue_comment_event("edited"), delivery="c1"
        )

        assert response.json()["status"] == "accepted"
        assert len(threads.posts) == before + 1
        assert threads.revisions == [], "it rewrote a message that was never posted"

    async def test_an_edit_of_a_message_somebody_deleted_is_dropped(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """A message a person removed from their thread is not one to put back under them, so no
        replacement is posted. It tried, which is the part worth pinning: `revise_calls` records
        the ask and `revisions` only the ones that found something.
        """
        await self.mirrored(tracked)
        thread_id = thread_for(threads, 98)
        threads.forget_message(thread_id, note_message(threads, thread_id))
        before = len(threads.posts)

        response = await deliver(
            tracked, "issue_comment", payloads.issue_comment_event("edited"), delivery="c2"
        )

        assert response.json()["status"] == "accepted"
        assert threads.revise_calls, "it never even looked for the message"
        assert threads.revisions == [], "it rewrote something that was not there"
        assert len(threads.posts) == before, "a deleted message was replaced"

    async def test_an_edit_of_a_note_mirrored_before_the_id_was_kept_is_dropped(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Every row written before this column existed. The id was thrown away and GitHub's
        comment says nothing about which Discord message holds it, so the edit cannot be shown and
        must not be posted a second time under the stale one. It self-heals as notes arrive.
        """
        await self.mirrored(tracked)
        await db_session.execute(update(MirroredNote).values(discord_message_id=None))
        await db_session.commit()
        before = len(threads.posts)

        response = await deliver(
            tracked, "issue_comment", payloads.issue_comment_event("edited"), delivery="c2"
        )

        assert response.json()["status"] == "accepted"
        assert threads.revise_calls == [], "it asked Discord about a message it had no id for"
        assert len(threads.posts) == before

    async def test_the_message_id_is_recorded_when_the_note_lands(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Without this every test above is unreachable in production. The id exists only in the
        instant the post returns, so it is written down then or not at all."""
        await self.mirrored(tracked)

        recorded = await db_session.scalar(select(MirroredNote.discord_message_id))
        assert recorded == note_message(threads, thread_for(threads, 98))

    async def test_a_redelivered_edit_is_harmless(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """No claim is taken for an edit and none needs to be: writing the same content over the
        same message twice is the same as writing it once, which is why an at-least-once queue
        needs no extra guard here. The webhook ledger stops a repeat of one delivery id, so this
        sends the same edit under two.
        """
        await self.mirrored(tracked)
        payload = payloads.issue_comment_event("edited", body="the same correction")

        await deliver(tracked, "issue_comment", payload, delivery="c2")
        await deliver(tracked, "issue_comment", payload, delivery="c3")

        assert len(threads.revisions) == 2
        assert threads.revisions[0] == threads.revisions[1]

    async def test_the_thread_is_left_shut_after_an_edit(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """Rewriting reopens an archived thread exactly as posting does, so it has to be shut
        again afterwards. Without this, editing a comment on a closed issue would drag its thread
        back into the channel and leave it there - and people edit comments after closing.
        """
        await self.mirrored(tracked)
        await deliver(
            tracked,
            "issues",
            payloads.issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z"),
            delivery="i1",
        )
        thread_id = thread_for(threads, 98)
        assert threads.threads[thread_id].archived is True

        await deliver(
            tracked, "issue_comment", payloads.issue_comment_event("edited"), delivery="c2"
        )

        assert threads.revisions, "nothing was rewritten, so this proves nothing"
        assert threads.unarchived[-1] == thread_id, "the rewrite did not reopen the thread to land"
        assert threads.threads[thread_id].archived is True, "the rewrite left the thread open"


async def test_a_comment_on_a_closed_issue_lands_and_leaves_the_thread_shut(
    tracked: AsyncClient, threads: FakeThreadGateway
) -> None:
    """Both halves, because each without the other is a bug this change could have shipped.

    Closing shuts the thread and the bot still has to be able to write to it, which is the half
    that was always true. The new half is what happens afterwards: posting is what reopens an
    archived thread, so without putting it back every late comment would drag a closed issue's
    thread into the channel and leave it there. People comment after closing constantly.
    """
    await deliver(
        tracked,
        "issues",
        payloads.issue_event("closed", state="closed", closed_at="2026-08-11T12:00:00Z"),
        delivery="i1",
    )
    thread_id = threads.created[0].thread_id
    assert threads.threads[thread_id].archived is True
    before = len(threads.posts)

    response = await deliver(
        tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1"
    )

    assert response.json()["status"] == "accepted"
    assert len(threads.posts) == before + 1
    assert threads.unarchived[-1] == thread_id, "the comment did not reopen the thread to land"
    assert threads.threads[thread_id].archived is True, "the comment left the thread open"


async def test_the_number_of_tracked_items_never_changes(
    tracked: AsyncClient, db_session: AsyncSession
) -> None:
    await deliver(tracked, "issue_comment", payloads.issue_comment_event(), delivery="c1")

    assert await db_session.scalar(select(func.count()).select_from(TrackedItem)) == 2


class TestReviewMirroring:
    async def test_a_submitted_review_reaches_the_pull_request_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        response = await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert response.json()["status"] == "accepted"
        assert threads.posts[-1][0] == thread_for(threads, 99)
        assert "**monalisa** approved this pull request" in threads.posts[-1][1]

    async def test_changes_requested_reaches_the_thread(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(state="changes_requested"),
            delivery="r1",
        )

        assert "requested changes" in threads.posts[-1][1]

    async def test_an_approval_with_no_body_still_lands(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)

        await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event(body=""),
            delivery="r1",
        )

        assert len(threads.posts) == before + 1
        assert "approved this pull request" in threads.posts[-1][1]

    async def test_a_linked_reviewer_is_mentioned(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        await UserLinkStore(db_session).link(
            guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=606
        )
        await db_session.commit()

        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert "<@606>" in threads.posts[-1][1]

    async def test_a_review_on_an_untracked_pull_request_is_ignored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)
        payload = payloads.pull_request_review_event()
        payload["pull_request"]["number"] = 999

        await deliver(tracked, "pull_request_review", payload, delivery="r1")

        assert await tracked.outcome_of("r1") == "ignored"
        assert len(threads.posts) == before

    async def test_an_edited_review_rewrites_its_message(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        """The third of the three note kinds. A review body is a comment with a verdict on it and
        shares the mirror, so the same gate change covers it."""
        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )
        before = len(threads.posts)

        response = await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event("edited", body="On reflection, one nit"),
            delivery="r2",
        )

        assert response.json()["status"] == "accepted"
        assert len(threads.posts) == before, "the edit was posted as a second message"
        assert "On reflection, one nit" in threads.revisions[-1][2]

    async def test_a_dismissed_review_is_not_mirrored(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)

        response = await deliver(
            tracked,
            "pull_request_review",
            payloads.pull_request_review_event("dismissed"),
            delivery="r1",
        )

        assert response.json()["status"] == "ignored"
        assert len(threads.posts) == before

    async def test_a_repeated_review_delivery_posts_once(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        before = len(threads.posts)
        payload = payloads.pull_request_review_event()

        first = await deliver(tracked, "pull_request_review", payload, delivery="r1")
        second = await deliver(tracked, "pull_request_review", payload, delivery="r1")

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"
        assert len(threads.posts) == before + 1

    async def test_a_review_never_touches_the_metadata_message(
        self, tracked: AsyncClient, threads: FakeThreadGateway
    ) -> None:
        thread_id = thread_for(threads, 99)
        before = threads.metadata_of(thread_id)

        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        assert threads.metadata_of(thread_id) == before

    async def test_a_review_does_not_move_the_workflow_status(
        self, tracked: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Approving is not /SET_READY_FOR_MERGE. Status commands arrive in MVP 3."""
        await deliver(
            tracked, "pull_request_review", payloads.pull_request_review_event(), delivery="r1"
        )

        db_session.expunge_all()
        item = await db_session.scalar(
            select(TrackedItem).where(TrackedItem.github_object_type == ObjectType.PR)
        )
        assert item is not None
        assert item.status is Status.NOT_REVIEWED


class TestNoteTargeting:
    async def test_a_comment_is_matched_on_kind_as_well_as_number(
        self, tracked: AsyncClient, db_session: AsyncSession, threads: FakeThreadGateway
    ) -> None:
        """Numbers are unique per repository on GitHub, so this can only bite on bad data.

        Pinning the kind means a comment can never be handed the other sort of item.
        """
        from shannon.db.stores.repositories import RepositoryStore
        from shannon.db.stores.tracked_items import TrackedItemStore

        repository = await RepositoryStore(db_session).get_by_github_id(payloads.REPO_ID)
        assert repository is not None
        items = TrackedItemStore(db_session)

        issue = await items.get_by_number(
            repository_id=repository.id, number=12, object_type=ObjectType.ISSUE
        )
        pull = await items.get_by_number(
            repository_id=repository.id, number=7, object_type=ObjectType.PR
        )

        assert issue is not None and issue.github_object_type is ObjectType.ISSUE
        assert pull is not None and pull.github_object_type is ObjectType.PR
        assert issue.id != pull.id

    async def test_asking_for_the_wrong_kind_finds_nothing(
        self, tracked: AsyncClient, db_session: AsyncSession
    ) -> None:
        from shannon.db.stores.repositories import RepositoryStore
        from shannon.db.stores.tracked_items import TrackedItemStore

        repository = await RepositoryStore(db_session).get_by_github_id(payloads.REPO_ID)
        assert repository is not None

        wrong = await TrackedItemStore(db_session).get_by_number(
            repository_id=repository.id, number=12, object_type=ObjectType.PR
        )

        assert wrong is None


async def test_a_payload_the_parser_refuses_stops_before_anything_runs(
    registered: Repository,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
) -> None:
    """`then` closes a review request, so it must not run for a note that was never read.

    The check sits ahead of it deliberately: it is the one place the handler can answer without
    touching the database at all. A DELETED comment is the example since issue #165, because an
    edited one is mirrored now - the parser still refuses the actions that reach no thread, and
    those must stop before the hook rather than after it.
    """
    mirror = ItemNoteMirror(
        db_sessionmaker,
        threads,
        render=lambda note, mentions, roles: Panel.of_text("hello"),
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
    )
    ran: list[object] = []

    async def then(snapshot: object) -> None:
        ran.append(snapshot)

    handler = build_note_handler(mirror, parse_comment_event, then=then)
    outcome = await handler("deleted", payloads.issue_comment_event())

    assert outcome == "ignored"
    assert ran == []
    assert threads.posts == []


class TestARetryAfterTheCommentLanded:
    """A retry re-runs the whole handler, and posting a message cannot be undone.

    The step beside the mirror runs first for that reason, so a failure in it costs nothing but
    a repeat. What the repeat must not do is post the comment again, and the two tests below are
    the two ways round it: the step failing before the post, and the post having already landed.
    """

    async def test_the_step_beside_it_failing_first_posts_nothing(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        mirror = ItemNoteMirror(
            db_sessionmaker,
            threads,
            render=lambda note, mentions, roles: Panel.of_text("hello"),
            shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        )
        failures = _FailsAfterTheNote()
        handler = build_note_handler(mirror, parse_comment_event, then=failures)

        with pytest.raises(RuntimeError):
            await handler("created", payloads.issue_comment_event())

        assert threads.posts == [], "the post ran before the step that failed, so it is owed"

        await handler("created", payloads.issue_comment_event())

        assert [content for _, content in threads.posts].count("hello") == 1

    async def test_a_handler_run_again_after_the_post_landed_does_not_post_twice(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        """The half the test above cannot reach, and the one the claim exists for.

        Nothing inside the handler runs after the post, so nothing there can fail once the
        comment has landed. What re-runs it is the queue: a delivery whose status could not be
        written stays leased, comes back when the lease runs out, and is handled again from the
        top. From here that is simply the handler being called twice.
        """
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        mirror = ItemNoteMirror(
            db_sessionmaker,
            threads,
            render=lambda note, mentions, roles: Panel.of_text("hello"),
            shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        )
        handler = build_note_handler(mirror, parse_comment_event)

        await handler("created", payloads.issue_comment_event())
        await handler("created", payloads.issue_comment_event())

        assert [content for _, content in threads.posts].count("hello") == 1

    async def test_the_step_after_it_still_runs(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        issue_event,
    ) -> None:
        issues = build_item_sync(db_sessionmaker, threads, IssuePolicy())
        await issues.sync(issue_event("opened"))
        mirror = ItemNoteMirror(
            db_sessionmaker,
            threads,
            render=lambda note, mentions, roles: Panel.of_text("hello"),
            shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        )
        seen: list[object] = []

        async def record(snapshot: object) -> None:
            seen.append(snapshot)

        handler = build_note_handler(mirror, parse_comment_event, then=record)
        await handler("created", payloads.issue_comment_event())

        assert len(seen) == 1
        assert [content for _, content in threads.posts].count("hello") == 1


class _FailsAfterTheNote:
    """Whatever runs alongside the mirror, failing the first time it is asked.

    Beside it rather than after it, whatever the name says: the handler runs this first so that
    the one step it cannot undo is the last thing it does.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, snapshot: object) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("the database went away")
