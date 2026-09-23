"""Saying so in the thread once every review a pull request asked for has come back approving.

Issue #155. The condition is the whole difficulty, and it is not answerable from anything this
bot stores: no verdict is kept, the rows recording who was asked are deleted as GitHub drops
them, and a review is rewritten in place when it is dismissed, which is an action this bot does
not subscribe to. So it is asked of GitHub, twice, and only ever on an approval.

Two refusals carry this file. A pull request with somebody still outstanding says nothing —
GitHub empties `requested_reviewers` as people submit, so what is left is who has not answered.
And a review that is not an approval says nothing and costs no GitHub calls at all, which is both
what keeps the calls off the ordinary review and what stops the bot announcing agreement on a
pull request nobody approved: every verdict in an empty mapping is an approval.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import MirroredNote
from shannon.discord_bot.formatting import format_everyone_approved, format_review
from shannon.domain.models import Actor, ReviewSnapshot
from shannon.github import mapping
from shannon.github.webhooks.reviews import parse_review_event
from shannon.services.notes import ItemNoteMirror, build_note_handler
from shannon.services.reviews import EveryoneApprovedLine, is_worth_a_message
from shannon.services.sync.announcements import Arrival
from shannon.services.sync.shutting import KeepsThreadsShut
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import register_repository
from tests.support.stack import DeliveryClient, build_http_client, build_stack, deliver

pytestmark = pytest.mark.integration

REPO_FULL = f"{payloads.OWNER}/{payloads.REPO}".lower()
HEADING = "### 🏁 Approved"
OTHER_SHA = "d" * 40


def a_review(review_id: int, state: str, login: str = "monalisa") -> ReviewSnapshot:
    """One row as GitHub's reviews endpoint answers it, which is what the fake hands back."""
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    return ReviewSnapshot(
        repository=repo,
        item_number=7,
        review_id=review_id,
        state=state,
        author=Actor(login, 200),
        body="",
        html_url="",
        created_at=datetime(2026, 8, 11, 11, 0, tzinfo=UTC),
    )


def a_github(**overrides: Any) -> FakeGitHubClient:
    """A GitHub holding the pull request, and one approval on it from the only asked reviewer.

    Nobody outstanding by default, because the interesting refusals are the ones that put
    somebody back.
    """
    repo = mapping.repository(payloads.repository())
    assert repo is not None
    overrides.setdefault("requested_reviewers", [])
    snapshot = mapping.pull_request(payloads.pull_request(**overrides), repo)
    assert snapshot is not None
    github = FakeGitHubClient(pull_requests={(REPO_FULL, 7): snapshot})
    github.reviews[(REPO_FULL, 7)] = [a_review(1, "APPROVED")]
    return github


@pytest_asyncio.fixture
async def tracked(
    db_engine: AsyncEngine, db_session: AsyncSession, threads: FakeThreadGateway
) -> AsyncIterator[tuple[DeliveryClient, FakeGitHubClient]]:
    """A pull request that already has its thread, and a GitHub that answers about it."""
    await register_repository(db_session, guild_id=1, channel_id=99)
    github = a_github()
    async with build_http_client(build_stack(db_engine, threads=threads, github=github)) as client:
        await deliver(client, "pull_request", payloads.pull_request_event("opened"), delivery="p0")
        yield client, github


def line_over(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
) -> EveryoneApprovedLine:
    return EveryoneApprovedLine(
        db_sessionmaker,
        threads,
        github=github,
        render=format_everyone_approved,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
    )


def handler_over(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    threads: FakeThreadGateway,
    github: FakeGitHubClient,
):
    """The review route as the container wires it, so the order of the two hooks is under test."""
    mirror = ItemNoteMirror(
        db_sessionmaker,
        threads,
        render=format_review,
        shut_again=KeepsThreadsShut(db_sessionmaker, threads),
        worth_posting=is_worth_a_message,
    )
    return build_note_handler(
        mirror, parse_review_event, after=line_over(db_sessionmaker, threads, github).after_a_review
    )


def said(threads: FakeThreadGateway) -> list[str]:
    return [body for _, body in threads.posts]


class TestWhenItSpeaks:
    async def test_every_review_approving_names_the_author_and_the_assignees(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The pair a pull request waits on once the reviewing is done. The default payload has
        octocat as the author and hubot assigned."""
        _, github = tracked

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        rounded_up = [body for body in said(threads) if HEADING in body]
        assert len(rounded_up) == 1
        assert "octocat" in rounded_up[0]
        assert "hubot" in rounded_up[0]

    async def test_it_counts_the_approvals_rather_than_naming_them(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Each approver is already named one message above by the line that mirrored their
        review, and naming them again through a live mention map would ring the last of them
        about their own approval."""
        _, github = tracked
        github.reviews[(REPO_FULL, 7)] = [
            a_review(1, "APPROVED", "monalisa"),
            a_review(2, "APPROVED", "alice"),
        ]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        rounded_up = next(body for body in said(threads) if HEADING in body)
        assert "2 reviews" in rounded_up
        assert "alice" not in rounded_up

    async def test_a_pull_request_nobody_was_asked_to_review_still_says_it(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A spontaneous approval is vacuously everyone agreeing, and it is the answer the author
        was waiting for — more so, because there was no round to wait on."""
        _, github = tracked

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert any(HEADING in body for body in said(threads))


class TestWhenItStaysQuiet:
    async def test_a_review_that_is_not_an_approval_costs_no_github_calls(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The gate, and the reason it is first. Most reviews on a busy repository are comments,
        and asking GitHub two questions about each of them would be the cost of this feature."""
        _, github = tracked

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event(state="changes_requested")
        )

        assert not any(HEADING in body for body in said(threads))
        assert github.review_calls == []
        assert github.pull_request_calls == []

    async def test_somebody_still_being_waited_on_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """GitHub empties `requested_reviewers` as people submit, so anybody left in it is
        somebody who has not answered."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(
            requested_reviewers=[payloads.user("alice", 900)]
        ).pull_requests[(REPO_FULL, 7)]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_a_team_still_being_waited_on_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """No payload says who is in a team or which member satisfied its request, so a team
        still listed is the only evidence there is that somebody has not answered."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(
            requested_teams=[{"slug": "backend", "name": "Backend"}]
        ).pull_requests[(REPO_FULL, 7)]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_one_dissenting_verdict_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The case the whole reduction exists for. Both have answered, so nobody is outstanding,
        and one of them asked for changes."""
        _, github = tracked
        github.reviews[(REPO_FULL, 7)] = [
            a_review(1, "CHANGES_REQUESTED", "alice"),
            a_review(2, "APPROVED", "monalisa"),
        ]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_a_reduction_with_nothing_in_it_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The belt to the gate. Every verdict in an empty mapping is an approval, so without
        this the bot would announce agreement on a pull request nobody had approved."""
        _, github = tracked
        github.reviews[(REPO_FULL, 7)] = []

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_a_draft_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """A draft has asked nobody yet, so there is no round for an approval to finish."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(draft=True).pull_requests[(REPO_FULL, 7)]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_a_merged_pull_request_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """People approve out of courtesy after a merge, and posting reopens an archived
        thread."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(
            state="closed", merged=True, merged_at="2026-08-11T12:00:00Z"
        ).pull_requests[(REPO_FULL, 7)]

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))

    async def test_a_pull_request_with_no_head_commit_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
        db_session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The claim would become the constant `approved:`, which is one claim for the whole life
        of the item: it would announce once and then stay silent for every later round."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(head={"ref": "feature"}).pull_requests[
            (REPO_FULL, 7)
        ]

        with caplog.at_level("WARNING"):
            await handler_over(db_sessionmaker, threads, github)(
                "submitted", payloads.pull_request_review_event()
            )

        assert not any(HEADING in body for body in said(threads))
        assert "no head commit" in caplog.text
        claimed = (await db_session.scalars(select(MirroredNote))).all()
        assert not any(note.note_key.startswith("approved:") for note in claimed)

    async def test_a_pull_request_github_no_longer_has_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        _, github = tracked
        github.reviews[(REPO_FULL, 7)] = None

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )

        assert not any(HEADING in body for body in said(threads))


class TestItSaysItOnce:
    async def test_the_same_delivery_twice_posts_one_round_up(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The queue is at-least-once, so a delivery whose status could not be written is handled
        again from the top."""
        _, github = tracked
        handler = handler_over(db_sessionmaker, threads, github)

        await handler("submitted", payloads.pull_request_review_event())
        await handler("submitted", payloads.pull_request_review_event())

        assert len([body for body in said(threads) if HEADING in body]) == 1

    async def test_a_new_commit_gets_its_own_round_up(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """The claim keys on the head this was evaluated against, so a push that invalidates the
        approvals and a fresh round says so again. Approve, push, approve."""
        _, github = tracked
        handler = handler_over(db_sessionmaker, threads, github)

        await handler("submitted", payloads.pull_request_review_event())
        github.pull_requests[(REPO_FULL, 7)] = a_github(
            head={"ref": "feature", "sha": OTHER_SHA}
        ).pull_requests[(REPO_FULL, 7)]
        github.reviews[(REPO_FULL, 7)] = [a_review(2, "APPROVED")]
        await handler("submitted", payloads.pull_request_review_event(id=999))

        assert len([body for body in said(threads) if HEADING in body]) == 2


class TestWhereItCannotFindTheItem:
    """Reachable only by calling the line directly. It runs after the mirror has posted, which
    has already proved the repository is registered and the item has a thread — but the two reads
    are separate transactions and nothing holds the item still between them.
    """

    async def test_an_unregistered_repository_says_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        snapshot = parse_review_event("submitted", payloads.pull_request_review_event())
        assert snapshot is not None

        await line_over(db_sessionmaker, threads, a_github()).after_a_review(snapshot)

        assert said(threads) == []

    async def test_an_item_this_server_does_not_track_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        _, github = tracked
        payload = payloads.pull_request_review_event()
        payload["pull_request"]["number"] = 999
        snapshot = parse_review_event("submitted", payload)
        assert snapshot is not None
        posted = len(threads.posts)

        await line_over(db_sessionmaker, threads, github).after_a_review(snapshot)

        assert len(threads.posts) == posted


class TestTheOtherMomentItBecomesTrue:
    """Withdrawing the last outstanding review request, which no review can ever announce.

    Alice approves while Bob is still asked, so nothing is said. The author then cancels Bob's
    request — and at that instant everybody remaining has approved, with no further review coming
    to notice it. Before this the thread simply never said so.
    """

    def _arrival(self, github: FakeGitHubClient, action: str = "review_request_removed") -> Arrival:
        snapshot = github.pull_requests[(REPO_FULL, 7)]
        return Arrival(
            action=action,
            snapshot=snapshot,
            payload=payloads.pull_request_event(action),
            tracked_item_id=1,
            thread_id=1,
            arrived=1,
        )

    async def test_withdrawing_the_last_request_says_it(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        _, github = tracked

        await line_over(db_sessionmaker, threads, github).say(self._arrival(github))

        assert any(HEADING in body for body in said(threads))

    async def test_withdrawing_one_of_several_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Somebody is still outstanding, so the round is not over."""
        _, github = tracked
        github.pull_requests[(REPO_FULL, 7)] = a_github(
            requested_reviewers=[payloads.user("alice", 900)]
        ).pull_requests[(REPO_FULL, 7)]

        await line_over(db_sessionmaker, threads, github).say(self._arrival(github))

        assert not any(HEADING in body for body in said(threads))

    async def test_any_other_delivery_says_nothing_and_asks_github_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """This sits in the tuple every pull request delivery runs through, so the action check
        is what keeps two GitHub calls off all of them."""
        _, github = tracked

        await line_over(db_sessionmaker, threads, github).say(
            self._arrival(github, action="synchronize")
        )

        assert not any(HEADING in body for body in said(threads))
        assert github.pull_request_calls == []
        assert github.review_calls == []

    async def test_an_issue_delivery_says_nothing(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """One tuple of announcers serves the issues handler too, and an issue has no review
        requests to withdraw. Handed the action anyway, so the type half of the guard is what
        answers rather than the action half."""
        _, github = tracked
        repo = mapping.repository(payloads.repository())
        assert repo is not None
        issue = mapping.issue(payloads.issue(), repo)
        assert issue is not None
        arrival = Arrival(
            action="review_request_removed",
            snapshot=issue,
            payload=payloads.issue_event("opened"),
            tracked_item_id=1,
            thread_id=1,
            arrived=1,
        )

        await line_over(db_sessionmaker, threads, github).say(arrival)

        assert not any(HEADING in body for body in said(threads))

    async def test_it_shares_its_claim_with_the_review_route(
        self,
        tracked: tuple[DeliveryClient, FakeGitHubClient],
        db_sessionmaker: async_sessionmaker[AsyncSession],
        threads: FakeThreadGateway,
    ) -> None:
        """Both moments can land on one agreed state — the last approval arrives, and a stale
        outstanding request is tidied up after it. One claim on the head means the second finds
        it taken rather than saying the same thing twice."""
        _, github = tracked
        line = line_over(db_sessionmaker, threads, github)

        await handler_over(db_sessionmaker, threads, github)(
            "submitted", payloads.pull_request_review_event()
        )
        await line.say(self._arrival(github))

        assert len([body for body in said(threads) if HEADING in body]) == 1
