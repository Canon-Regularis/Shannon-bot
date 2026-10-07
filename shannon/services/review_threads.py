"""Who a reply on a diff answers, asked of GitHub when the reply arrives. Issue #231.

Answering somebody's review comment rang nobody but whoever was named in the answer. A reply's
webhook carries `in_reply_to_id` - the comment its thread opened with - and nothing about who wrote
that or who else has answered since, and nothing in this project keeps a note's author. So the
thread is read from GitHub. Asked rather than remembered, which also reaches every thread that was
already open before this was written, the one the issue reports among them, and needs nothing new
stored.

Who a reply answers is everybody who wrote in its thread before it: the person who opened it, then
everybody who answered, once each. Not only the opener, because a review thread is a conversation:
when the reviewer answers the author's answer, the opener is the person replying and the one being
answered is the author. Whoever is replying is left out, which is issue #230's rule - they know what
they wrote.

GitHub not answering is weighed by what it means. A pull request it no longer has is final, and the
reply goes out naming nobody. A refusal is not going to change on a retry either, so the reply goes
out without the people it answers rather than not at all. An outage or a rate limit usually passes,
so a young reply is held back for it: the delivery fails before anything is claimed and comes round
again. But only while the reply is young - a delivery is given up after two hours of trying, and
holding out for the ping that long would cost the reply itself. A read that has not answered in time
counts as an outage too. The delivery has a deadline of its own, and a read that ran into it would
fail every attempt without ever reaching the question of how old the reply is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from shannon.domain.models import Actor, ItemNote, ReviewCommentSnapshot
from shannon.github.client import ReadsReviewComments
from shannon.github.errors import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.mentions import MENTION_LIMIT

logger = logging.getLogger(__name__)

# How long a reply is held back for a GitHub that cannot be reached, counted from when it was
# written. Long enough to ride out the blips that make up nearly every failure, so the person it
# answers is rung late rather than not at all; short enough that a real outage costs the ping and
# not the reply, which waits for nothing past this. Read when it is needed rather than bound at
# construction, so a test can move it.
REPLY_WAITS_FOR_GITHUB = timedelta(minutes=30)


def everyone_it_answers(
    reply: ReviewCommentSnapshot, thread: Sequence[ReviewCommentSnapshot]
) -> tuple[Actor, ...]:
    """Everybody who wrote in this reply's thread before it, opener first, minus the replier.

    `thread` is every inline comment on the pull request, which is what GitHub can be asked for;
    the reply's own thread is picked out of it here.

    Earlier by id rather than by time. An id is never missing, GitHub hands them out in order, and
    a reply delivered late - or read again for an edit - must not be said to answer people who
    only wrote after it. Sorted here rather than taken in the order the page came in, because that
    order is GitHub's and nothing promised it.

    Once each, by the lowered login, so somebody who answered three times is named once; capped
    like the names a body writes, so a long thread cannot reach the whole server. Nothing in a
    thread is older than the comment it opened with, so the opener comes first and the cap never
    drops the person whose comment started it.
    """
    root = reply.in_reply_to_id
    if root is None:
        return ()
    # Documented as the comment the thread opened with, whichever one in it the reply was typed
    # under. Followed one step anyway: nothing here checks that GitHub keeps to it, and a reply
    # under a reply would otherwise find a thread of one.
    under = next((comment for comment in thread if comment.comment_id == root), None)
    if under is not None and under.in_reply_to_id is not None:
        root = under.in_reply_to_id

    earlier = sorted(
        (
            comment
            for comment in thread
            if root in (comment.comment_id, comment.in_reply_to_id)
            and comment.comment_id < reply.comment_id
        ),
        key=lambda comment: comment.comment_id,
    )

    replying = reply.author.login.lower() if reply.author else None
    answered: dict[str, Actor] = {}
    for comment in earlier:
        # Nobody to ring where GitHub has lost the account, and no reason to ring the replier.
        if comment.author is None or comment.author.login.lower() == replying:
            continue
        answered.setdefault(comment.author.login.lower(), comment.author)
    return tuple(answered.values())[:MENTION_LIMIT]


class ReviewThreads:
    """Fills in who a reply on a diff answers, for the inline-comment mirror to name and ring."""

    def __init__(
        self,
        github: ReadsReviewComments,
        *,
        read_within: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._github = github
        # How long the read may take, which has to leave the rest of the delivery room inside
        # the deadline the worker gives it: the claim and the post still come after this.
        self._read_within = read_within
        self._now = now

    async def answering(self, note: ItemNote) -> ItemNote:
        """The note with whoever it answers filled in, or the note as it came.

        As it came for anything that is not a reply on a diff, which asks GitHub nothing: most
        inline comments open a thread, and the other two kinds of note never answer one.

        Raises only for a GitHub that cannot be reached, or does not answer in time, while the
        reply is young enough to wait for it - before anything is claimed, so the delivery comes
        round again and is posted then.
        """
        if not isinstance(note, ReviewCommentSnapshot) or note.in_reply_to_id is None:
            return note
        try:
            async with asyncio.timeout(self._read_within.total_seconds()):
                thread = await self._github.list_review_comments(note.repository, note.item_number)
        except (GitHubUnavailableError, GitHubRateLimitError, TimeoutError):
            if self._worth_waiting_for(note):
                raise
            logger.warning(
                "GitHub could not list the inline comments on %s#%s and the reply there is too "
                "old to wait any longer, so it is posted without ringing whoever it answers",
                note.repository.full_name,
                note.item_number,
                exc_info=True,
            )
            return note
        except (GitHubAuthError, GitHubRefusedError):
            logger.warning(
                "GitHub refused to list the inline comments on %s#%s, so the reply there is "
                "posted without ringing whoever it answers",
                note.repository.full_name,
                note.item_number,
                exc_info=True,
            )
            return note
        if thread is None:
            logger.info(
                "GitHub no longer has %s#%s, so the reply there names nobody it answers",
                note.repository.full_name,
                note.item_number,
            )
            return note
        return replace(note, replying_to=everyone_it_answers(note, thread))

    def _worth_waiting_for(self, note: ReviewCommentSnapshot) -> bool:
        """Whether the reply is still young enough to hold back for GitHub.

        A reply GitHub gave no time for is treated as old: waiting on a clock that cannot be read
        is the one way here to lose the reply rather than the ping.
        """
        if note.created_at is None:
            return False
        return self._now() - note.created_at < REPLY_WAITS_FOR_GITHUB
