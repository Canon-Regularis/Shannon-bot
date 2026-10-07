"""Who a reply on a diff answers, worked out from the thread GitHub lists. Issue #231.

The case the issue reports, on orthant-kcpc/kcpc-workshops#77: beedware asked for a change on a
line, mkutay answered "done", and beedware was never told. The pure half is the thread arithmetic,
so it is tested here without a database; the class half is the one read and what happens when
GitHub will not answer it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shannon.domain.enums import ObjectType
from shannon.domain.models import Actor, CommentSnapshot, RepositorySnapshot, ReviewCommentSnapshot
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.mentions import MENTION_LIMIT
from shannon.services import review_threads
from shannon.services.review_threads import ReviewThreads, everyone_it_answers
from tests.fakes.github import FakeGitHubClient

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="orthant-kcpc",
    name="kcpc-workshops",
    html_url="https://github.com/orthant-kcpc/kcpc-workshops",
)
NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
# Generous: only the tests about a read that runs out of time pass anything shorter.
READ_WITHIN = timedelta(seconds=5)

# GitHub's ids for the people below. Carried because the mirror resolves people by them.
ACCOUNTS = {"beedware": 3001, "mkutay": 4001, "nemeth06": 5001, "octocat": 583231}


def by(login: str) -> Actor:
    return Actor(login, ACCOUNTS.get(login.lower()))


def comment(
    comment_id: int, login: str | None, *, under: int | None = None
) -> ReviewCommentSnapshot:
    """One inline comment on #77, written at `NOW`. `under` is its `in_reply_to_id`; None opens a
    thread."""
    return ReviewCommentSnapshot(
        repository=REPO,
        item_number=77,
        comment_id=comment_id,
        html_url=f"https://github.com/orthant-kcpc/kcpc-workshops/pull/77#discussion_r{comment_id}",
        body="",
        path="Week 2 Problems.tex",
        line=246,
        in_reply_to_id=under,
        author=by(login) if login is not None else None,
        created_at=NOW,
    )


def logins(people: tuple[Actor, ...]) -> list[str]:
    return [person.login for person in people]


# The thread from the issue: beedware opened it and mkutay answered.
OPENED = comment(4197046428, "beedware")
DONE = comment(4197154572, "mkutay", under=OPENED.comment_id)


class TestWhoAReplyAnswers:
    def test_the_comment_a_reply_is_under(self) -> None:
        """The case the issue reports, exactly."""
        assert logins(everyone_it_answers(DONE, [OPENED, DONE])) == ["beedware"]

    def test_answering_the_answer_rings_the_other_side(self) -> None:
        """Why it is everybody in the thread and not only whoever opened it. When beedware
        answers "done", the opener is the person replying, and the one being answered is mkutay."""
        thanks = comment(4197200000, "beedware", under=OPENED.comment_id)

        assert logins(everyone_it_answers(thanks, [OPENED, DONE, thanks])) == ["mkutay"]

    def test_everybody_before_it_opener_first_then_in_order(self) -> None:
        later = comment(4197160000, "nemeth06", under=OPENED.comment_id)
        reply = comment(4197170000, "octocat", under=OPENED.comment_id)

        assert logins(everyone_it_answers(reply, [OPENED, DONE, later, reply])) == [
            "beedware",
            "mkutay",
            "nemeth06",
        ]

    def test_githubs_order_is_not_trusted(self) -> None:
        later = comment(4197160000, "nemeth06", under=OPENED.comment_id)
        reply = comment(4197170000, "octocat", under=OPENED.comment_id)

        assert logins(everyone_it_answers(reply, [reply, later, DONE, OPENED])) == [
            "beedware",
            "mkutay",
            "nemeth06",
        ]

    def test_somebody_who_wrote_twice_is_named_once_whatever_the_case(self) -> None:
        again = comment(4197150000, "BeedWare", under=OPENED.comment_id)

        assert logins(everyone_it_answers(DONE, [OPENED, again, DONE])) == ["beedware"]

    def test_whoever_is_replying_is_left_out_even_when_they_opened_the_thread(self) -> None:
        mine = comment(10, "mkutay")
        answered = comment(11, "beedware", under=10)
        back = comment(12, "mkutay", under=10)

        assert logins(everyone_it_answers(back, [mine, answered, back])) == ["beedware"]

    def test_a_reply_in_a_thread_of_your_own_answers_nobody(self) -> None:
        mine = comment(10, "mkutay")
        more = comment(11, "mkutay", under=10)

        assert everyone_it_answers(more, [mine, more]) == ()

    def test_the_replier_is_matched_whatever_the_case(self) -> None:
        """On both sides of the comparison: the thread can carry the capitals as well as the
        reply, and lowering only one side lets the replier back in as somebody they answer."""
        opened = comment(10, "BeedWare")
        shouted = comment(11, "BEEDWARE", under=10)
        quiet = comment(12, "beedware", under=10)

        assert everyone_it_answers(shouted, [opened, shouted]) == ()
        assert everyone_it_answers(quiet, [opened, quiet]) == ()

    def test_an_account_github_has_lost_is_skipped(self) -> None:
        """GitHub sends a null user for a deleted account. There is nobody to ring."""
        gone = comment(10, None)
        answered = comment(11, "nemeth06", under=10)
        reply = comment(12, "mkutay", under=10)

        assert logins(everyone_it_answers(reply, [gone, answered, reply])) == ["nemeth06"]

    def test_a_reply_from_an_account_github_has_lost_still_names_the_thread(self) -> None:
        reply = comment(4197154572, None, under=OPENED.comment_id)

        assert logins(everyone_it_answers(reply, [OPENED, reply])) == ["beedware"]

    def test_replies_written_after_it_are_not_who_it_answers(self) -> None:
        """A reply delivered late, or read again for an edit, finds a thread that has moved on."""
        after = comment(4197160000, "nemeth06", under=OPENED.comment_id)

        assert logins(everyone_it_answers(DONE, [OPENED, DONE, after])) == ["beedware"]

    def test_another_thread_on_the_same_pull_request_is_not_this_one(self) -> None:
        elsewhere = comment(4197000000, "nemeth06")
        answered_there = comment(4197050000, "octocat", under=elsewhere.comment_id)

        assert logins(everyone_it_answers(DONE, [elsewhere, OPENED, answered_there, DONE])) == [
            "beedware"
        ]

    def test_a_reply_under_a_reply_still_finds_the_whole_thread(self) -> None:
        """GitHub documents `in_reply_to_id` as the opener. Followed one step anyway."""
        answered = comment(11, "nemeth06", under=10)
        reply = comment(12, "mkutay", under=11)

        assert logins(everyone_it_answers(reply, [comment(10, "beedware"), answered, reply])) == [
            "beedware",
            "nemeth06",
        ]

    def test_an_opener_github_no_longer_lists_leaves_the_rest_named(self) -> None:
        answered = comment(11, "nemeth06", under=10)
        reply = comment(12, "mkutay", under=10)

        assert logins(everyone_it_answers(reply, [answered, reply])) == ["nemeth06"]

    def test_a_crowd_is_cut_at_the_mention_limit_with_the_opener_kept(self) -> None:
        """Handed over newest first, so a cap that kept whatever came first would drop the
        person whose comment started it."""
        crowd = [comment(10 + index, f"person{index}", under=10) for index in range(1, 13)]
        reply = comment(99, "mkutay", under=10)

        answered = everyone_it_answers(reply, [*reversed(crowd), comment(10, "beedware"), reply])

        assert len(answered) == MENTION_LIMIT
        assert logins(answered) == ["beedware", *(f"person{index}" for index in range(1, 10))]

    def test_a_comment_that_opens_a_thread_answers_nobody(self) -> None:
        assert everyone_it_answers(OPENED, [OPENED, DONE]) == ()


def threads_reading(stocked: list[ReviewCommentSnapshot] | None) -> FakeGitHubClient:
    github = FakeGitHubClient()
    github.review_comments[(REPO.full_name.lower(), 77)] = stocked
    return github


class TestReadingTheThread:
    async def test_a_reply_comes_back_naming_whoever_it_answers(self) -> None:
        github = threads_reading([OPENED, DONE])

        answered = await ReviewThreads(github, read_within=READ_WITHIN, now=lambda: NOW).answering(
            DONE
        )

        assert isinstance(answered, ReviewCommentSnapshot)
        assert logins(answered.replying_to) == ["beedware"]
        assert github.review_comment_calls == [("orthant-kcpc/kcpc-workshops", 77)]

    async def test_a_comment_that_opens_a_thread_asks_github_nothing(self) -> None:
        """Most inline comments open a thread, and none of them may pay for a read."""
        github = threads_reading([OPENED])

        assert await ReviewThreads(github, read_within=READ_WITHIN).answering(OPENED) is OPENED
        assert github.review_comment_calls == []

    async def test_a_note_that_is_not_on_a_diff_is_handed_back_untouched(self) -> None:
        github = threads_reading([OPENED])
        note = CommentSnapshot(
            repository=REPO,
            item_number=77,
            comment_id=1,
            html_url="https://github.com/orthant-kcpc/kcpc-workshops/pull/77#issuecomment-1",
            body="looks good",
            object_type=ObjectType.PR,
        )

        assert await ReviewThreads(github, read_within=READ_WITHIN).answering(note) is note
        assert github.review_comment_calls == []

    async def test_a_pull_request_github_no_longer_has_answers_nobody(self) -> None:
        answered = await ReviewThreads(
            threads_reading(None), read_within=READ_WITHIN, now=lambda: NOW
        ).answering(DONE)

        assert answered is DONE


class TestWhenGitHubWillNotSay:
    """An outage is waited out while the reply is young; a refusal never is."""

    @pytest.mark.parametrize(
        "error",
        [
            GitHubUnavailableError("GitHub is down"),
            GitHubRateLimitError("slow down", retry_after=60),
        ],
    )
    async def test_a_young_reply_waits_for_github(self, error: GitHubError) -> None:
        """Raised before anything is claimed, so the delivery comes round again and the person
        it answers is rung late rather than never."""
        github = threads_reading([OPENED, DONE])
        github.review_comment_error = error

        with pytest.raises(type(error)):
            await ReviewThreads(github, read_within=READ_WITHIN, now=lambda: NOW).answering(DONE)

    @pytest.mark.parametrize(
        "error",
        [
            GitHubUnavailableError("GitHub is down"),
            GitHubRateLimitError("slow down", retry_after=60),
        ],
    )
    async def test_an_old_reply_goes_out_without_them(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A delivery is given up after two hours of trying, which would cost the reply itself."""
        github = threads_reading([OPENED, DONE])
        github.review_comment_error = error
        later = NOW + review_threads.REPLY_WAITS_FOR_GITHUB

        with caplog.at_level(logging.WARNING, logger="shannon.services.review_threads"):
            answered = await ReviewThreads(
                github, read_within=READ_WITHIN, now=lambda: later
            ).answering(DONE)

        assert answered is DONE
        assert "too old to wait" in caplog.text

    async def test_a_reply_just_inside_the_wait_still_waits(self) -> None:
        github = threads_reading([OPENED, DONE])
        github.review_comment_error = GitHubUnavailableError("GitHub is down")
        nearly = NOW + review_threads.REPLY_WAITS_FOR_GITHUB - timedelta(seconds=1)

        with pytest.raises(GitHubUnavailableError):
            await ReviewThreads(github, read_within=READ_WITHIN, now=lambda: nearly).answering(DONE)

    async def test_a_reply_with_no_time_on_it_is_never_held_back(self) -> None:
        """Waiting on a clock that cannot be read is the one way to lose the reply itself."""
        github = threads_reading([OPENED, DONE])
        github.review_comment_error = GitHubUnavailableError("GitHub is down")
        undated = replace(DONE, created_at=None)

        assert (
            await ReviewThreads(github, read_within=READ_WITHIN, now=lambda: NOW).answering(undated)
            is undated
        )

    @pytest.mark.parametrize("error", [GitHubAuthError("no token"), GitHubRefusedError("will not")])
    async def test_a_refusal_never_holds_a_reply_back(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        """However young the reply: a refusal will say the same thing on every retry."""
        github = threads_reading([OPENED, DONE])
        github.review_comment_error = error

        with caplog.at_level(logging.WARNING, logger="shannon.services.review_threads"):
            answered = await ReviewThreads(
                github, read_within=READ_WITHIN, now=lambda: NOW
            ).answering(DONE)

        assert answered is DONE
        assert "refused to list" in caplog.text


class SlowGitHub:
    """A GitHub that answers, eventually: long after the read was allowed to wait."""

    async def list_review_comments(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewCommentSnapshot] | None:
        await asyncio.sleep(5)
        return [OPENED, DONE]


class TestARead:
    """Found reviewing #231. The delivery has a deadline of its own, and a read that ran into it
    failed every attempt without ever being asked how old the reply was - so the reply itself
    was dropped after two hours, which the wait exists to prevent."""

    async def test_that_runs_out_of_time_holds_a_young_reply_back(self) -> None:
        threads = ReviewThreads(
            SlowGitHub(), read_within=timedelta(milliseconds=10), now=lambda: NOW
        )

        with pytest.raises(TimeoutError):
            await threads.answering(DONE)

    async def test_that_runs_out_of_time_lets_an_old_reply_go_without_them(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        later = NOW + review_threads.REPLY_WAITS_FOR_GITHUB
        threads = ReviewThreads(
            SlowGitHub(), read_within=timedelta(milliseconds=10), now=lambda: later
        )

        with caplog.at_level(logging.WARNING, logger="shannon.services.review_threads"):
            answered = await threads.answering(DONE)

        assert answered is DONE
        assert "too old to wait" in caplog.text
