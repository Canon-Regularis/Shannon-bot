"""Announcing what landed on a pull request when somebody pushes.

The only announcer that reads GitHub, and so the only one that can fail for a reason outside
this process. It makes up to eleven calls and runs last in the chain for that reason.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.json import JsonObject
from shannon.domain.models import Actor, Commit, CommitRange, CommitRef
from shannon.github import mapping
from shannon.github.client import ReadsCommits
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)

# How many commits one push is allowed to say out loud. The worker gives a delivery sixty
# seconds, and this is ten GitHub reads plus ten Discord posts on top of the sync that ran first.
COMMITS_PER_PUSH = 10

PUSHED = "synchronize"

# GitHub's compare statuses for a head that is no longer a descendant of the base. `behind` is
# the one that matters most: a `reset --hard HEAD~3 && push --force` leaves nothing ahead and
# `total_commits` at zero.
REWRITTEN = frozenset({"behind", "diverged"})

# The only status worth reading commits off. `identical` is a no-op force push of the same tree.
AHEAD = "ahead"

# Git's null ref: forty zeros mean there was nothing there. It cannot happen on a pull request,
# whose branch existed before the push, but the value is read off a payload.
_NOTHING = "0" * 40

Renderer = Callable[[Commit], Panel]
PushRenderer = Callable[[Actor | None], Panel]
CountRenderer = Callable[[int], Panel]


class CommitLine:
    """Posts a message per commit into a pull request's thread when somebody pushes.

    A push payload carries the two ends of the range and nothing else, so everything a reader
    sees here is fetched. Notes are keyed on the commit's SHA rather than the delivery, so a
    retry after a partial batch says the commits that did not land; the cost is that a branch
    carrying somebody's own earlier work is announced again in the thread it is merged into.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        github: ReadsCommits,
        *,
        render: Renderer,
        rewritten: PushRenderer,
        left: CountRenderer,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._line = ClaimedLine(sessionmaker, threads, shut_again)
        self._github = github
        self._render = render
        self._rewritten = rewritten
        self._left = left

    async def say(self, arrival: Arrival) -> None:
        """Announce what this push did.

        Each commit is read, claimed and posted before the next one is started, so a batch
        cancelled on the worker's deadline keeps everything it got through.
        """
        if arrival.action != PUSHED:
            return

        ends = _ends(arrival.payload)
        if ends is None:
            logger.warning(
                "a push on tracked item %s carried no usable range, so nothing is said about it",
                arrival.tracked_item_id,
            )
            return

        repository = arrival.snapshot.repository
        pushed_by = mapping.actor(arrival.payload.get("sender"))
        compared = await self._github.compare_commits(repository.owner, repository.name, *ends)
        if compared is None:
            logger.info(
                "GitHub had no compare for the push on tracked item %s", arrival.tracked_item_id
            )
            return

        if compared.status in REWRITTEN:
            # Once, about the rewrite rather than its commits: every commit on a rebased branch
            # has a new SHA and would otherwise be announced as new work. Keyed on the delivery,
            # so two force pushes in a row say two things.
            await self._line.say_once(
                tracked_item_id=arrival.tracked_item_id,
                thread_id=arrival.thread_id,
                note_key=f"force-push:{arrival.arrived}",
                panel=self._rewritten(pushed_by),
            )
            return

        if compared.status != AHEAD:
            logger.info(
                "the push on tracked item %s left the branch %s, so there is nothing to say",
                arrival.tracked_item_id,
                compared.status,
            )
            return

        theirs = [
            commit for commit in compared.commits if _is_the_pushers_own_work(commit, pushed_by)
        ]
        # The newest, not the first: a compare returns oldest first, and a long branch catching
        # up puts other people's older commits at the front of the range.
        wanted = theirs[-COMMITS_PER_PUSH:]

        said = 0
        for commit in wanted:
            if await self._say_one(arrival, repository.owner, repository.name, commit):
                said += 1

        await self._say_what_was_left(arrival, compared, kept=len(theirs), said=said)

    async def _say_one(self, arrival: Arrival, owner: str, name: str, ref: CommitRef) -> bool:
        """One commit, read and posted, or False for one GitHub could not be read.

        The numbers are a call each because the commit rows inside a compare carry no `stats`
        block. A commit that has gone between the compare and this read was rewritten away, and
        is skipped rather than failing the whole delivery.
        """
        stats = await self._github.commit_stats(owner, name, ref.sha)
        if stats is None:
            logger.info("GitHub had no numbers for %s, so it is not announced", ref.sha)
            return False

        commit = Commit(sha=ref.sha, message=ref.message, author=ref.author, stats=stats)
        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=commit.note_key,
            panel=self._render(commit),
        )
        return True

    async def _say_what_was_left(
        self, arrival: Arrival, compared: CommitRange, *, kept: int, said: int
    ) -> None:
        """A footnote counting the commits this would have announced and did not.

        Counted from what was kept rather than from GitHub's total: pulling main in brings
        dozens of other people's commits that were never going to be announced. The unlisted
        rows are added back, because a compare stops at 250 commits while `total_commits` keeps
        counting.
        """
        unlisted = max(compared.total - len(compared.commits), 0)
        left = kept - said + unlisted
        if left <= 0:
            return

        # One count, without saying whether the cap, a merge or a failed read left them out.
        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=f"commits-left:{arrival.arrived}",
            panel=self._left(left),
        )


def _ends(payload: JsonObject) -> tuple[str, str] | None:
    before = payload.get("before")
    after = payload.get("after")
    for end in (before, after):
        if not isinstance(end, str) or not end or end == _NOTHING:
            return None
    return before, after  # type: ignore[return-value]


def _is_the_pushers_own_work(commit: CommitRef, pushed_by: Actor | None) -> bool:
    """Whether this commit is the pusher's own work, and so worth announcing.

    A merge is dropped: it carries somebody else's whole branch behind it, and a pull request
    kept up to date with main would post one every time. A commit whose author has no GitHub
    account is kept, because the account is null whenever a git address is not registered to a
    profile and dropping those would swallow somebody's commits silently.
    """
    if commit.merge:
        return False
    if commit.author is None or pushed_by is None:
        return True
    return commit.author.login.casefold() == pushed_by.login.casefold()
