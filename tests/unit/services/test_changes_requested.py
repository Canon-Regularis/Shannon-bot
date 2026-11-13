"""Who a review asking for changes asks, worked out from what GitHub lists. Issue #263.

The pure half is the arithmetic: which files a review commented on, who made each of them on this
pull request, and how many of those fit in one message. The class half is the reads, in order,
and what a review says when GitHub will not answer one of them - which is the one-line shape at
once for a refusal, and a held-back delivery for an outage while the review is young enough.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shannon.discord_bot.formatting import FILE_LINES_BUDGET, file_lines_width, format_review
from shannon.discord_bot.panels import BlockKind
from shannon.discord_bot.safe_text import REVIEW_PATH_LIMIT
from shannon.domain.enums import ObjectType
from shannon.domain.models import (
    Actor,
    ChangesAsked,
    CommentSnapshot,
    CommitRef,
    FileAuthors,
    ItemNote,
    RepositorySnapshot,
    ReviewCommentSnapshot,
    ReviewSnapshot,
)
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.services import changes_requested, review_threads
from shannon.services.audience import PEOPLE_RUNG
from shannon.services.changes_requested import (
    AUTHORS_NAMED,
    COMMITS_LISTED,
    FILES_NAMED,
    ChangesRequested,
    authors_of,
    files_in,
    named,
)
from tests.fakes.github import FakeGitHubClient

REPO = RepositorySnapshot(
    github_repo_id=1,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url="https://github.com/Canon-Regularis/Shannon-bot",
)
KEY = ("canon-regularis/shannon-bot", 7)
NOW = datetime(2026, 11, 12, 9, tzinfo=UTC)
# Generous: only the tests about a read that runs out of time pass anything shorter.
READ_WITHIN = timedelta(seconds=5)
LATER = NOW + review_threads.REPLY_WAITS_FOR_GITHUB

OCTOCAT = Actor("octocat", 583231)
HUBOT = Actor("hubot", 100)
DEFUNKT = Actor("defunkt", 200)
MONALISA = Actor("monalisa", 300)

# The pull request's commits, in GitHub's order: octocat's, hubot's, a merge monalisa pressed,
# one written under an address no account holds, and defunkt's. The review is on the last.
FIRST = CommitRef(sha="1" * 40, message="Add the hook", author=OCTOCAT, merge=False)
SECOND = CommitRef(sha="2" * 40, message="Read the files", author=HUBOT, merge=False)
MERGED = CommitRef(sha="3" * 40, message="Merge main", author=MONALISA, merge=True)
NOBODYS = CommitRef(sha="4" * 40, message="Fix a typo", author=None, merge=False)
HEAD = CommitRef(sha="5" * 40, message="Name the files", author=DEFUNKT, merge=False)
COMMITS = [FIRST, SECOND, MERGED, NOBODYS, HEAD]
# A commit on the branch the pull request came from, which every file's history runs into.
MAIN = CommitRef(sha="9" * 40, message="Start", author=Actor("torvalds", 1024), merge=False)

REVIEW = ReviewSnapshot(
    repository=REPO,
    item_number=7,
    review_id=555,
    html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#pullrequestreview-555",
    body="The lock leaks on cancellation.",
    state="changes_requested",
    author=MONALISA,
    created_at=NOW,
    commit_id=HEAD.sha,
    pull_request_author=OCTOCAT,
)


def comment(comment_id: int, path: str, *, review_id: int | None = 555) -> ReviewCommentSnapshot:
    return ReviewCommentSnapshot(
        repository=REPO,
        item_number=7,
        comment_id=comment_id,
        html_url=f"https://github.com/Canon-Regularis/Shannon-bot/pull/7#discussion_r{comment_id}",
        body="Not here.",
        path=path,
        review_id=review_id,
        author=MONALISA,
        created_at=NOW,
    )


def logins(people: Sequence[Actor]) -> list[str]:
    return [person.login for person in people]


def github_with(
    *,
    commits: Sequence[CommitRef] | None = tuple(COMMITS),
    comments: Sequence[ReviewCommentSnapshot] | None = (),
    files: dict[str, Sequence[CommitRef] | None] | None = None,
    at: str = HEAD.sha,
) -> FakeGitHubClient:
    """A GitHub stocked with this pull request's commits, its inline comments, and each file's
    history read back from `at`."""
    github = FakeGitHubClient()
    if commits is not None:
        github.pull_request_commits[KEY] = commits
    github.review_comments[KEY] = comments
    for path, history in (files or {}).items():
        github.file_history[(KEY[0], at, path)] = history
    return github


def asked_of(note: ItemNote) -> ChangesAsked:
    assert isinstance(note, ReviewSnapshot)
    assert note.changes_asked is not None
    return note.changes_asked


def changes(github: FakeGitHubClient, *, now: datetime = NOW) -> ChangesRequested:
    return ChangesRequested(github, read_within=READ_WITHIN, now=lambda: now)


def two_files() -> FakeGitHubClient:
    """Two files the review commented on: the hook octocat and defunkt made, and the README hubot
    made. Both histories run on into the branch the pull request came from."""
    return github_with(
        comments=[comment(11, "shannon/hook.py"), comment(12, "README.md")],
        files={"shannon/hook.py": [HEAD, FIRST, MAIN], "README.md": [SECOND, MAIN]},
    )


class TestWhichFilesAReviewIsOn:
    def test_only_the_reviews_own_comments_count(self) -> None:
        """Every inline comment on the pull request comes back, other reviews' among them."""
        comments = [comment(1, "a.py"), comment(2, "b.py", review_id=444), comment(3, "c.py")]

        assert files_in(comments, 555) == ("a.py", "c.py")

    def test_a_comment_that_came_with_no_review_counts_for_none(self) -> None:
        assert files_in([comment(1, "a.py", review_id=None)], 555) == ()

    def test_each_file_once_in_the_order_it_was_commented_on(self) -> None:
        """By id rather than by the page, whose order is GitHub's."""
        comments = [comment(30, "b.py"), comment(10, "a.py"), comment(20, "b.py")]

        assert files_in(comments, 555) == ("a.py", "b.py")

    def test_a_comment_with_no_path_names_no_file(self) -> None:
        assert files_in([comment(1, ""), comment(2, "a.py")], 555) == ("a.py",)

    def test_no_comments_name_no_files(self) -> None:
        assert files_in([], 555) == ()


class TestWhoMadeAFile:
    def test_the_pull_requests_commits_that_touched_it_in_its_order(self) -> None:
        """The history runs on into the branch the pull request came from, which made none of
        this pull request's work."""
        touched = frozenset({HEAD.sha, FIRST.sha, MAIN.sha})

        assert logins(authors_of(touched, COMMITS)) == ["octocat", "defunkt"]

    def test_a_merge_or_a_commit_with_no_account_makes_nobody_an_author(self) -> None:
        touched = frozenset({MERGED.sha, NOBODYS.sha})

        assert authors_of(touched, COMMITS) == ()

    def test_somebody_with_two_commits_on_it_is_named_once_whatever_the_case(self) -> None:
        again = CommitRef(sha="6" * 40, message="Again", author=Actor("OctoCat"), merge=False)
        touched = frozenset({FIRST.sha, again.sha})

        assert [login.lower() for login in logins(authors_of(touched, [FIRST, again]))] == [
            "octocat"
        ]

    def test_a_file_nothing_on_the_pull_request_touched_was_made_by_nobody(self) -> None:
        assert authors_of(frozenset(), COMMITS) == ()


def people(count: int, prefix: str = "person") -> tuple[Actor, ...]:
    return tuple(Actor(f"{prefix}{number}", number) for number in range(count))


class TestHowManyEachFileNames:
    def test_one_file_with_eight_authors_names_all_eight(self) -> None:
        """Found reviewing the design: five a file would leave three people unrung on a message
        with room to ring fifty."""
        asked = named([FileAuthors("a.py", people(8))])

        assert asked.files == (FileAuthors("a.py", people(8), 0),)
        assert len(asked.rung) == 8

    def test_ten_files_name_five_each_and_count_the_rest(self) -> None:
        made = [FileAuthors(f"{number}.py", people(7, f"f{number}-")) for number in range(10)]

        asked = named(made)

        assert [(len(file.authors), file.more) for file in asked.files] == [(5, 2)] * 10
        assert asked.more_files == 0

    def test_fewer_files_share_what_one_message_may_ring(self) -> None:
        made = [FileAuthors(f"{number}.py", people(20, f"f{number}-")) for number in range(3)]

        asked = named(made)

        share = PEOPLE_RUNG // 3
        assert [(len(file.authors), file.more) for file in asked.files] == [(share, 20 - share)] * 3

    def test_the_smallest_shares_of_every_file_fit_in_what_one_message_may_ring(self) -> None:
        """Why the shares need no cap of their own: past FILES_NAMED there are no file lines."""
        assert FILES_NAMED * AUTHORS_NAMED <= PEOPLE_RUNG

    def test_a_file_nobody_can_be_named_for_keeps_its_line(self) -> None:
        asked = named([FileAuthors("a.py"), FileAuthors("b.py", (HUBOT,))])

        assert asked.files == (FileAuthors("a.py"), FileAuthors("b.py", (HUBOT,)))
        assert asked.rung == (HUBOT,)

    def test_no_files_name_nobody(self) -> None:
        assert named([]) == ChangesAsked()


LONGEST_LOGIN = 39


def login(number: int) -> str:
    """A login as long as GitHub allows, never linked, which is the widest a name is written."""
    return f"{number:02d}".ljust(LONGEST_LOGIN, "a")


def longest_path(number: int) -> str:
    return f"{number:02d}/".ljust(REVIEW_PATH_LIMIT - 3, "p") + ".py"


class TestTheLinesFitTheMessage:
    """The critique's case: lines allowed to run on cost the body, which is the review."""

    def test_the_longest_files_and_names_stay_inside_the_budget(self) -> None:
        made = [
            FileAuthors(longest_path(file), tuple(Actor(login(10 * file + n)) for n in range(5)))
            for file in range(10)
        ]

        asked = named(made)

        assert file_lines_width(asked) <= FILE_LINES_BUDGET
        assert len(asked.files) + asked.more_files == 10
        assert asked.more_files > 0
        # Never a line naming nobody for a file somebody made: the file goes to the count instead.
        assert all(file.authors for file in asked.files)
        # A line short of room names fewer and counts the rest, rather than losing the file.
        assert any(len(file.authors) < AUTHORS_NAMED for file in asked.files)
        assert all(len(file.authors) + file.more == 5 for file in asked.files)

    def test_only_the_people_shown_are_rung(self) -> None:
        made = [
            FileAuthors(longest_path(file), tuple(Actor(login(10 * file + n)) for n in range(5)))
            for file in range(10)
        ]

        asked = named(made)

        shown = [person for file in asked.files for person in file.authors]
        assert asked.rung == tuple(shown)

    def test_the_review_keeps_its_words(self) -> None:
        made = [
            FileAuthors(longest_path(file), tuple(Actor(login(10 * file + n)) for n in range(5)))
            for file in range(10)
        ]
        review = replace(
            REVIEW,
            author=Actor(login(99)),
            body="The lock leaks on cancellation. " * 22,
            changes_asked=named(made),
        )

        card = format_review(review, {})

        assert card.trimmed() == card
        assert [block.kind for block in card.blocks] == [
            BlockKind.HEADING,
            BlockKind.FIELDS,
            BlockKind.BODY,
            BlockKind.FOOTNOTE,
        ]


class TestAReviewThatAsksNoChanges:
    """Approvals have their own line and a review that only comments asks nobody anything, so
    neither may pay for a read."""

    @pytest.mark.parametrize("state", ["approved", "commented", "dismissed"])
    async def test_asks_github_nothing(self, state: str) -> None:
        github = two_files()
        review = replace(REVIEW, state=state)

        assert await changes(github).answering(review) is review
        assert github.commit_list_calls == []
        assert github.review_comment_calls == []
        assert github.file_history_calls == []

    async def test_a_note_that_is_not_a_review_is_handed_back_untouched(self) -> None:
        github = two_files()
        note = CommentSnapshot(
            repository=REPO,
            item_number=7,
            comment_id=1,
            html_url="https://github.com/Canon-Regularis/Shannon-bot/pull/7#issuecomment-1",
            body="looks good",
            object_type=ObjectType.PR,
        )

        assert await changes(github).answering(note) is note
        assert github.commit_list_calls == []


class TestOnTheFiles:
    async def test_each_file_names_who_made_it_on_the_pull_request(self) -> None:
        github = two_files()

        asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(
            files=(
                FileAuthors("shannon/hook.py", (OCTOCAT, DEFUNKT)),
                FileAuthors("README.md", (HUBOT,)),
            )
        )
        assert logins(asked.rung) == ["octocat", "defunkt", "hubot"]

    async def test_each_history_is_read_back_from_the_reviewed_commit(self) -> None:
        github = two_files()

        await changes(github).answering(REVIEW)

        assert github.commit_list_calls == [KEY]
        assert github.review_comment_calls == [KEY]
        assert github.file_history_calls == [
            (KEY[0], HEAD.sha, "shannon/hook.py"),
            (KEY[0], HEAD.sha, "README.md"),
        ]

    async def test_a_reviewer_who_made_a_file_stays_named_on_it(self) -> None:
        """The line says who made the file; the mirror is what keeps them from being rung."""
        github = github_with(
            comments=[comment(11, "shannon/hook.py")],
            files={"shannon/hook.py": [HEAD]},
        )
        review = replace(REVIEW, author=DEFUNKT)

        asked = asked_of(await changes(github).answering(review))

        assert asked.files == (FileAuthors("shannon/hook.py", (DEFUNKT,)),)

    async def test_a_file_only_merges_touched_is_still_named(self) -> None:
        """As made by somebody unknown, which is not the same as the review asking nobody."""
        github = github_with(comments=[comment(11, "a.py")], files={"a.py": [MERGED, MAIN]})

        asked = asked_of(await changes(github).answering(REVIEW))

        assert asked.files == (FileAuthors("a.py"),)

    async def test_as_many_files_as_are_named(self) -> None:
        paths = [f"{number}.py" for number in range(FILES_NAMED)]
        github = github_with(
            comments=[comment(number, path) for number, path in enumerate(paths)],
            files=dict.fromkeys(paths, (HEAD,)),
        )

        asked = asked_of(await changes(github).answering(REVIEW))

        assert [file.path for file in asked.files] == paths


class TestInOneLine:
    """The pull request's author and everybody who committed, minus whoever is reviewing."""

    async def test_a_review_with_no_inline_comments(self) -> None:
        github = github_with()

        asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert github.file_history_calls == []

    async def test_a_review_whose_comments_are_all_another_reviews(self) -> None:
        github = github_with(comments=[comment(11, "a.py", review_id=444)])

        assert logins(asked_of(await changes(github).answering(REVIEW)).everyone) == [
            "octocat",
            "hubot",
            "defunkt",
        ]

    async def test_inline_comments_github_no_longer_has(self) -> None:
        github = github_with(comments=None)

        assert asked_of(await changes(github).answering(REVIEW)).everyone == (
            OCTOCAT,
            HUBOT,
            DEFUNKT,
        )

    async def test_more_files_than_are_named_reads_no_file(self) -> None:
        paths = [f"{number}.py" for number in range(FILES_NAMED + 1)]
        github = github_with(
            comments=[comment(number, path) for number, path in enumerate(paths)],
            files=dict.fromkeys(paths, (HEAD,)),
        )

        asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert github.file_history_calls == []

    async def test_a_review_github_gave_no_commit_for_reads_no_comments(self) -> None:
        github = two_files()

        asked = asked_of(await changes(github).answering(replace(REVIEW, commit_id="")))

        assert asked.everyone == (OCTOCAT, HUBOT, DEFUNKT)
        assert github.review_comment_calls == []
        assert github.file_history_calls == []

    async def test_a_commit_the_pull_request_no_longer_has(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A force-push since the review: history read from there matches nothing here."""
        github = two_files()

        with caplog.at_level(logging.INFO, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github).answering(replace(REVIEW, commit_id="f" * 40)))

        assert asked.everyone == (OCTOCAT, HUBOT, DEFUNKT)
        assert github.file_history_calls == []
        # Routine, so not said out loud.
        assert caplog.text == ""

    async def test_a_commit_past_what_github_lists_is_said_out_loud(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Found by the critique: GitHub lists 250 commits on a pull request, and a review on a
        later one looks exactly like a force-push from here."""
        listed = [
            CommitRef(sha=f"{number:040d}", message="Step", author=HUBOT, merge=False)
            for number in range(COMMITS_LISTED)
        ]
        github = github_with(commits=listed, comments=[comment(11, "a.py")])

        with caplog.at_level(logging.INFO, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github).answering(REVIEW))

        assert asked.everyone == (OCTOCAT, HUBOT)
        assert "past the 250 GitHub lists" in caplog.text

    async def test_no_commit_at_the_cap_is_not_said_to_be_past_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        listed = [
            CommitRef(sha=f"{number:040d}", message="Step", author=HUBOT, merge=False)
            for number in range(COMMITS_LISTED)
        ]
        github = github_with(commits=listed)

        with caplog.at_level(logging.INFO, logger="shannon.services.changes_requested"):
            await changes(github).answering(replace(REVIEW, commit_id=""))

        assert caplog.text == ""

    async def test_a_pull_request_github_no_longer_has_names_its_author(self) -> None:
        github = github_with(commits=None, comments=[comment(11, "a.py")])

        assert asked_of(await changes(github).answering(REVIEW)).everyone == (OCTOCAT,)

    async def test_whoever_is_reviewing_is_left_out(self) -> None:
        """Issue #230's rule: they know what they asked."""
        github = github_with()
        review = replace(REVIEW, author=Actor("HUBOT", 100))

        assert asked_of(await changes(github).answering(review)).everyone == (OCTOCAT, DEFUNKT)

    async def test_a_review_by_a_deleted_account_leaves_nobody_out(self) -> None:
        github = github_with()

        asked = asked_of(await changes(github).answering(replace(REVIEW, author=None)))

        assert asked.everyone == (OCTOCAT, HUBOT, DEFUNKT)


class TestNobodyToName:
    """The review reads as it always has, rather than as asking nobody."""

    async def test_a_reviewer_who_opened_and_wrote_all_of_it(self) -> None:
        github = github_with(commits=[replace(FIRST, author=MONALISA)])
        review = replace(REVIEW, commit_id=FIRST.sha, pull_request_author=MONALISA)

        assert await changes(github).answering(review) is review

    async def test_a_deleted_author_and_no_commits(self) -> None:
        github = github_with(commits=None)
        review = replace(REVIEW, pull_request_author=None)

        assert await changes(github).answering(review) is review


class TestWhenGitHubRefuses:
    """A refusal says the same on every retry, so the review goes out at once in one line."""

    @pytest.mark.parametrize("error", [GitHubAuthError("no token"), GitHubRefusedError("no")])
    async def test_the_commits_leave_only_whoever_opened_it(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        github = two_files()
        github.error = error

        with caplog.at_level(logging.WARNING, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT,))
        assert "refused to list the commits" in caplog.text

    @pytest.mark.parametrize("error", [GitHubAuthError("no token"), GitHubRefusedError("no")])
    async def test_the_comments_name_everybody(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        github = two_files()
        github.review_comment_error = error

        with caplog.at_level(logging.WARNING, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert "refused to list the inline comments" in caplog.text
        assert github.file_history_calls == []

    @pytest.mark.parametrize("error", [GitHubAuthError("no token"), GitHubRefusedError("no")])
    async def test_a_files_history_names_everybody_and_asks_about_no_more(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file GitHub will not show is a repository it will not show."""
        github = two_files()
        github.file_history_error = error

        with caplog.at_level(logging.WARNING, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert github.file_history_calls == [(KEY[0], HEAD.sha, "shannon/hook.py")]
        assert caplog.text.count("refused to say which commits touched") == 1

    async def test_a_refusal_is_asked_again_next_time(self) -> None:
        """Nothing refused is kept, so access granted later counts at once."""
        github = two_files()
        github.file_history_error = GitHubAuthError("no token")
        reviews = changes(github)
        await reviews.answering(REVIEW)

        github.file_history_error = None
        asked = asked_of(await reviews.answering(REVIEW))

        assert len(asked.files) == 2
        assert len(github.file_history_calls) == 3

    async def test_a_file_github_no_longer_has_names_everybody(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        github = github_with(
            comments=[comment(11, "gone.py"), comment(12, "README.md")],
            files={"README.md": [SECOND]},
        )
        reviews = changes(github)

        with caplog.at_level(logging.WARNING, logger="shannon.services.changes_requested"):
            asked = asked_of(await reviews.answering(REVIEW))
        await reviews.answering(REVIEW)

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert "no longer has gone.py" in caplog.text
        # Asked again the second time, and never about the file after it.
        assert github.file_history_calls == [(KEY[0], HEAD.sha, "gone.py")] * 2


class SlowFiles(FakeGitHubClient):
    """A GitHub that answers about files, eventually: long after the reads were allowed to wait."""

    async def list_commits_on_path(
        self, owner: str, name: str, *, sha: str, file_path: str
    ) -> Sequence[CommitRef] | None:
        await asyncio.sleep(5)
        return await super().list_commits_on_path(owner, name, sha=sha, file_path=file_path)


def slow_files() -> SlowFiles:
    github = SlowFiles()
    github.pull_request_commits[KEY] = COMMITS
    github.review_comments[KEY] = [comment(11, "a.py")]
    return github


TRANSIENT = [
    GitHubUnavailableError("GitHub is down"),
    GitHubRateLimitError("slow down", retry_after=60),
]


class TestWhenGitHubCannotBeReached:
    """Waited out while the review is young, then sent with whatever was read."""

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_a_young_review_waits_for_the_commits(self, error: GitHubError) -> None:
        """Raised before anything is claimed, so the delivery comes round again."""
        github = two_files()
        github.error = error

        with pytest.raises(type(error)):
            await changes(github).answering(REVIEW)

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_a_young_review_waits_for_the_inline_comments(self, error: GitHubError) -> None:
        """Not taken for a refusal, which would send it at once in the one line."""
        github = two_files()
        github.review_comment_error = error

        with pytest.raises(type(error)):
            await changes(github).answering(REVIEW)

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_a_young_review_waits_for_a_files_history(self, error: GitHubError) -> None:
        github = two_files()
        github.file_history_error = error

        with pytest.raises(type(error)):
            await changes(github).answering(REVIEW)

    async def test_a_young_review_waits_for_a_read_that_runs_out_of_time(self) -> None:
        reviews = ChangesRequested(
            slow_files(), read_within=timedelta(milliseconds=10), now=lambda: NOW
        )

        with pytest.raises(TimeoutError):
            await reviews.answering(REVIEW)

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_an_old_review_without_the_commits_names_whoever_opened_it(
        self, error: GitHubError, caplog: pytest.LogCaptureFixture
    ) -> None:
        github = two_files()
        github.error = error

        with caplog.at_level(logging.WARNING, logger="shannon.services.changes_requested"):
            asked = asked_of(await changes(github, now=LATER).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT,))
        assert "too old to wait" in caplog.text

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_an_old_review_without_the_inline_comments_names_everybody(
        self, error: GitHubError
    ) -> None:
        """The commits came back first, so everybody who made the pull request can be named."""
        github = two_files()
        github.review_comment_error = error

        asked = asked_of(await changes(github, now=LATER).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))
        assert github.file_history_calls == []

    @pytest.mark.parametrize("error", TRANSIENT)
    async def test_an_old_review_with_the_commits_names_everybody(self, error: GitHubError) -> None:
        github = two_files()
        github.file_history_error = error

        asked = asked_of(await changes(github, now=LATER).answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))

    async def test_an_old_review_whose_read_ran_out_of_time_names_everybody(self) -> None:
        """The commits that came back before the time ran out are still used."""
        reviews = ChangesRequested(
            slow_files(), read_within=timedelta(milliseconds=10), now=lambda: LATER
        )

        asked = asked_of(await reviews.answering(REVIEW))

        assert asked == ChangesAsked(everyone=(OCTOCAT, HUBOT, DEFUNKT))

    async def test_a_review_just_inside_the_wait_still_waits(self) -> None:
        github = two_files()
        github.error = GitHubUnavailableError("GitHub is down")
        nearly = LATER - timedelta(seconds=1)

        with pytest.raises(GitHubUnavailableError):
            await changes(github, now=nearly).answering(REVIEW)

    async def test_a_review_with_no_time_on_it_is_never_held_back(self) -> None:
        """Waiting on a clock that cannot be read is the one way to lose the review itself."""
        github = two_files()
        github.error = GitHubUnavailableError("GitHub is down")

        asked = asked_of(await changes(github).answering(replace(REVIEW, created_at=None)))

        assert asked == ChangesAsked(everyone=(OCTOCAT,))


class TestTheHistoriesKept:
    """A file's history read back from one commit never changes."""

    async def test_an_edit_on_the_same_commit_reads_each_file_once(self) -> None:
        github = two_files()
        reviews = changes(github)

        first = await reviews.answering(REVIEW)
        second = await reviews.answering(replace(REVIEW, body="The lock leaks. Fix it."))

        assert asked_of(first).files == asked_of(second).files
        assert len(github.file_history_calls) == 2
        # The commits and the comments are read every time, because they do change.
        assert len(github.commit_list_calls) == 2
        assert len(github.review_comment_calls) == 2

    async def test_a_file_nothing_touched_is_kept_too(self) -> None:
        github = github_with(comments=[comment(11, "a.py")], files={"a.py": []})
        reviews = changes(github)

        await reviews.answering(REVIEW)
        asked = asked_of(await reviews.answering(REVIEW))

        assert asked.files == (FileAuthors("a.py"),)
        assert len(github.file_history_calls) == 1

    async def test_a_review_on_another_commit_reads_again(self) -> None:
        github = github_with(comments=[comment(11, "a.py")], files={"a.py": [HEAD]})
        github.file_history[(KEY[0], SECOND.sha, "a.py")] = [SECOND]
        reviews = changes(github)

        await reviews.answering(REVIEW)
        asked = asked_of(await reviews.answering(replace(REVIEW, commit_id=SECOND.sha)))

        assert asked.files == (FileAuthors("a.py", (HUBOT,)),)
        assert github.file_history_calls == [
            (KEY[0], HEAD.sha, "a.py"),
            (KEY[0], SECOND.sha, "a.py"),
        ]

    async def test_the_oldest_goes_first_once_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three files read with room for two: the first one read is the one asked again."""
        monkeypatch.setattr(changes_requested, "HELD_FILES", 2)
        github = github_with(
            comments=[
                *(comment(number, path) for number, path in enumerate(["a.py", "b.py", "c.py"])),
                comment(20, "b.py", review_id=556),
                comment(21, "c.py", review_id=556),
                comment(30, "a.py", review_id=557),
            ],
            files=dict.fromkeys(["a.py", "b.py", "c.py"], (HEAD,)),
        )
        reviews = changes(github)

        await reviews.answering(REVIEW)
        await reviews.answering(replace(REVIEW, review_id=556))
        await reviews.answering(replace(REVIEW, review_id=557))

        assert [path for _, _, path in github.file_history_calls] == [
            "a.py",
            "b.py",
            "c.py",
            "a.py",
        ]
