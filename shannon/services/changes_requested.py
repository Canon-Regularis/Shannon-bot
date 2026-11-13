"""Who a review asking for changes asks, asked of GitHub when the review arrives. Issue #263.

A review asking for changes was posted into its thread and rang nobody but whoever its body named,
so it read like any other comment and nobody who had to act on it was told. A request for changes
is addressed to whoever has to make them, which is the people who made the code it is about.

Which code is the files its inline comments are on, and who made each of them is the pull
request's own commits that touched that file. Neither is on the review's webhook: the comments
arrive as deliveries of their own with no promised order, and a commit list says nothing of
files. So GitHub is asked, three ways, one after another: the pull request's commits, its inline
comments, picked out by the review they came in, and each file's history read back from the commit
the review was made on. Kept to the pull request's commits, that history is the file's authors on
this pull request and not on the branch it came from. One after another rather than all at once,
because GitHub answers a burst of reads with a limit of its own, and because the first refusal
stops the rest: a file GitHub will not show is a repository it will not show, and asking about
nine more files would cost nine reads for the same answer.

When the files cannot be told, the review says it asks the pull request's author and everybody
who committed to it, in one line. That is the shape for a review with no inline comments, for
more files than a message names, for a commit the pull request no longer has - a force-push, or a
pull request past the 250 commits GitHub lists - and for a history GitHub refused or no longer
has. Whoever is reviewing is left out of that line, which is issue #230's rule: they know what
they asked. In the file lines they stay named on a file they made, because the line says who made
it, and the mirror never rings a reviewer for their own review anyway.

GitHub not answering is weighed the way a reply on a diff weighs it (issue #231). A refusal will
say the same on every retry, so the review goes out at once in the one-line shape. An outage, a
rate limit or a read that ran out of time usually passes, so a young review is held back for it:
the delivery fails before anything is claimed and comes round again. Once the review is too old
to wait for, it goes out with whatever was read - everybody, if the commits came back, and the
pull request's author alone if not.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from shannon.discord_bot.formatting import FILE_LINES_BUDGET, file_lines_width
from shannon.domain.models import (
    Actor,
    ChangesAsked,
    CommitRef,
    FileAuthors,
    ItemNote,
    ReviewCommentSnapshot,
    ReviewSnapshot,
)
from shannon.github.client import ReadsFileHistory, ReadsReviewComments, ReadsWhoWroteIt
from shannon.github.errors import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.services.audience import PEOPLE_RUNG, author_and_committers
from shannon.services.review_threads import still_worth_waiting_for

logger = logging.getLogger(__name__)

# The most files a review is broken down by. Past this the one-line shape names everybody instead,
# without reading a single file's history: a review that touches this much of a pull request is
# about all of it, and a read per file would spend more than the breakdown is worth.
FILES_NAMED = 10

# The fewest people a file's line may name before the rest go to "and N more". More when there
# are fewer files, so one file with eight authors rings all eight: the share is PEOPLE_RUNG split
# between the files, and never less than this.
AUTHORS_NAMED = 5

# Together, the two above keep every file at its smallest share inside what one message may ring,
# FILES_NAMED * AUTHORS_NAMED <= PEOPLE_RUNG, and PEOPLE_RUNG split between the files never sums
# past it either. That is why the shares need no cap of their own, and a test holds the two to it.

# How many files' histories are kept. A file's history read back from one commit never changes, so
# nothing here expires; the bound is only so a bot that runs for months does not keep every file
# it was ever asked about. The oldest goes first.
HELD_FILES = 512

# The most commits GitHub lists on one pull request. A pull request at the cap may have its
# reviewed commit past the end of the list, which looks exactly like a force-push from here.
COMMITS_LISTED = 250


class ReadsWhoMadeIt(ReadsWhoWroteIt, ReadsReviewComments, ReadsFileHistory, Protocol):
    """The three reads a review asking for changes needs, and nothing that could write anything.

    One protocol rather than three handles, because one object answers all of them, the way
    `ReadsChecksAndItems` holds a check result's reads.
    """


def files_in(comments: Sequence[ReviewCommentSnapshot], review_id: int) -> tuple[str, ...]:
    """The files one review commented on, once each, in the order it commented on them.

    `comments` is every inline comment on the pull request, which is what GitHub can be asked
    for; the review's own are picked out by the review they came in. A reply this review left in
    an older thread came in with it, so its file counts. Ordered by id rather than by the page,
    because the page's order is GitHub's and nothing promised it. A comment with no path names no
    file.
    """
    own = sorted(
        (comment for comment in comments if comment.review_id == review_id and comment.path),
        key=lambda comment: comment.comment_id,
    )
    return tuple(dict.fromkeys(comment.path for comment in own))


def authors_of(touched: frozenset[str], commits: Sequence[CommitRef]) -> tuple[Actor, ...]:
    """Who made one file on this pull request: the authors of its commits that touched it.

    `touched` is the file's history read back from the reviewed commit, which runs past the pull
    request into the branch it came from; kept to the pull request's own commits, it is the work
    the review is asking about. Who counts as having written a commit is `author_and_committers`'s
    rule, so a merge or a commit with no account names nobody here either, and the order is the
    pull request's. Capped at PEOPLE_RUNG by that rule too, which a file's share never exceeds -
    one file alone gets all of it - so on a file with more authors than one message may ring, the
    ones past the cap are neither named nor counted in its "and N more".
    """
    return author_and_committers(None, [commit for commit in commits if commit.sha in touched])


def named(made: Sequence[FileAuthors]) -> ChangesAsked:
    """Each file with as many of its authors as the message has room for, the rest counted.

    `made` is each file with everybody who made it. A file gets its share of PEOPLE_RUNG, never
    less than AUTHORS_NAMED. The lines are then held to FILE_LINES_BUDGET, measured with every
    name at its widest, because the block after these lines is the reviewer's own words and gets
    only the room these leave. A line short of room names fewer people and
    counts the rest; one with no room for even one name, or for its `Unknown`, goes with every
    file after it to "and N more files". Only the people a line shows are rung.
    """
    share = max(AUTHORS_NAMED, PEOPLE_RUNG // max(len(made), 1))
    kept: list[FileAuthors] = []
    for index, file in enumerate(made):
        line = _fitting(kept, file, share, len(made) - index - 1)
        if line is None:
            return ChangesAsked(files=tuple(kept), more_files=len(made) - index)
        kept.append(line)
    return ChangesAsked(files=tuple(kept))


def _fitting(
    kept: Sequence[FileAuthors], file: FileAuthors, share: int, after: int
) -> FileAuthors | None:
    """The fullest line for `file` that still fits beside `kept`, or None when none does.

    Measured with the files `after` it counted on a line of their own, which is what is sent if
    this is the last line that fits. Never down to no names for a file that has authors: a line
    reading "made by 3 authors" says less than the "and N more files" it would cost.
    """
    least = 1 if file.authors else 0
    for count in range(min(share, len(file.authors)), least - 1, -1):
        line = FileAuthors(file.path, file.authors[:count], len(file.authors) - count)
        candidate = ChangesAsked(files=(*kept, line), more_files=after)
        if file_lines_width(candidate) <= FILE_LINES_BUDGET:
            return line
    return None


class ChangesRequested:
    """Fills in whom a review asking for changes asks, for the review mirror to name and ring."""

    def __init__(
        self,
        github: ReadsWhoMadeIt,
        *,
        read_within: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._github = github
        # How long the reads may take together, which has to leave the rest of the delivery room
        # inside the deadline the worker gives it: the claim and the post still come after this.
        self._read_within = read_within
        self._now = now
        # Each file's history as the SHAs that touched it, by repository, commit and path. Only
        # what GitHub answered is kept: a refusal is asked again, so access granted later counts
        # at once.
        self._held: dict[tuple[str, str, str], frozenset[str]] = {}

    async def answering(self, note: ItemNote) -> ItemNote:
        """The review with whom it asks filled in, or the note as it came.

        As it came for anything that is not a review asking for changes, which asks GitHub
        nothing: an approval has its own line, and a review that only comments asks nobody to
        change anything. As it came, too, when there is nobody to name at all, so the review
        reads as it always has rather than as asking nobody.

        Raises only for a GitHub that cannot be reached, or does not answer in time, while the
        review is young enough to wait for it - before anything is claimed, so the delivery comes
        round again and is posted then.
        """
        if not isinstance(note, ReviewSnapshot) or note.verdict != "changes_requested":
            return note
        # Outside the time limit, so a review too old to wait for can still name whoever the
        # commits that did come back say made the pull request.
        commits: Sequence[CommitRef] = ()
        try:
            async with asyncio.timeout(self._read_within.total_seconds()):
                commits = await self._commits(note)
                asked = await self._asked(note, commits)
        except (GitHubUnavailableError, GitHubRateLimitError, TimeoutError):
            if still_worth_waiting_for(note.created_at, self._now()):
                raise
            logger.warning(
                "GitHub could not say who made the files review %s on %s#%s asks changes on, and "
                "the review is too old to wait any longer, so it names everybody it can",
                note.review_id,
                note.repository.full_name,
                note.item_number,
                exc_info=True,
            )
            asked = ChangesAsked(everyone=_everyone(note, commits))
        if not asked.files and not asked.everyone:
            return note
        return replace(note, changes_asked=asked)

    async def _commits(self, note: ReviewSnapshot) -> Sequence[CommitRef]:
        """The pull request's commits, or none when GitHub refused them or no longer has it.

        None of them leaves only the pull request's author to name, and no commit for the review
        to have been made on, which is the one-line shape.
        """
        repository = note.repository
        try:
            commits = await self._github.list_pull_request_commits(
                repository.owner, repository.name, note.item_number
            )
        except (GitHubAuthError, GitHubRefusedError):
            logger.warning(
                "GitHub refused to list the commits on %s#%s, so review %s names only whoever "
                "opened it",
                repository.full_name,
                note.item_number,
                note.review_id,
                exc_info=True,
            )
            return ()
        return commits or ()

    async def _asked(self, note: ReviewSnapshot, commits: Sequence[CommitRef]) -> ChangesAsked:
        """Each file the review commented on with who made it, or everybody in one line."""
        everyone = ChangesAsked(everyone=_everyone(note, commits))
        if note.commit_id not in {commit.sha for commit in commits}:
            # A review GitHub gave no commit for lands here too: no commit has an empty SHA. Said
            # out loud only at the cap, so it is not mistaken for a force-push, which is routine.
            if note.commit_id and len(commits) >= COMMITS_LISTED:
                logger.info(
                    "Review %s on %s#%s was made on a commit past the %s GitHub lists, so it "
                    "names everybody rather than who made each file",
                    note.review_id,
                    note.repository.full_name,
                    note.item_number,
                    COMMITS_LISTED,
                )
            return everyone
        try:
            comments = await self._github.list_review_comments(note.repository, note.item_number)
        except (GitHubAuthError, GitHubRefusedError):
            logger.warning(
                "GitHub refused to list the inline comments on %s#%s, so review %s names "
                "everybody rather than who made each file",
                note.repository.full_name,
                note.item_number,
                note.review_id,
                exc_info=True,
            )
            return everyone
        paths = files_in(comments or (), note.review_id)
        if not paths or len(paths) > FILES_NAMED:
            return everyone
        made: list[FileAuthors] = []
        for path in paths:
            # One warning for the first file GitHub will not show, and no read for the rest.
            try:
                touched = await self._touched(note, path)
            except (GitHubAuthError, GitHubRefusedError):
                logger.warning(
                    "GitHub refused to say which commits touched %s on %s at %s, so review %s "
                    "names everybody rather than who made each file",
                    path,
                    note.repository.full_name,
                    note.commit_id,
                    note.review_id,
                    exc_info=True,
                )
                return everyone
            if touched is None:
                logger.warning(
                    "GitHub no longer has %s on %s at %s, so review %s names everybody rather "
                    "than who made each file",
                    path,
                    note.repository.full_name,
                    note.commit_id,
                    note.review_id,
                )
                return everyone
            made.append(FileAuthors(path, authors_of(touched, commits)))
        return named(made)

    async def _touched(self, note: ReviewSnapshot, path: str) -> frozenset[str] | None:
        """The SHAs that touched `path`, read back from the reviewed commit; None when GitHub no
        longer has it.

        Kept once read, because the history behind a commit never changes: an edit, a redelivery
        or a second review on the same commit reads the commits and the comments again, and not
        this.
        """
        repository = note.repository
        key = (repository.full_name.lower(), note.commit_id, path)
        held = self._held.get(key)
        if held is not None:
            return held
        history = await self._github.list_commits_on_path(
            repository.owner, repository.name, sha=note.commit_id, file_path=path
        )
        if history is None:
            return None
        touched = frozenset(commit.sha for commit in history)
        if len(self._held) >= HELD_FILES:
            self._held.pop(next(iter(self._held)))
        self._held[key] = touched
        return touched


def _everyone(note: ReviewSnapshot, commits: Sequence[CommitRef]) -> tuple[Actor, ...]:
    """The pull request's author and everybody who committed to it, minus whoever is reviewing.

    The reviewer is left out by lowered login, issue #230's rule: they know what they asked.
    """
    reviewer = note.author.login.lower() if note.author is not None else None
    people = author_and_committers(note.pull_request_author, commits)
    return tuple(person for person in people if person.login.lower() != reviewer)
