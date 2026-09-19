"""Saying what landed on a pull request, which nothing else in the thread does.

A thread says what the item is and what people said about it. Until issue #67 it said nothing at
all about the work: somebody could push five commits and the channel would look exactly as it did
before, because the delivery that says so was matched and dropped at the endpoint.

This is the only announcer that reads GitHub. The other two work entirely off the delivery they
were handed, so they cannot fail for a reason outside this process; this one makes up to eleven
calls and is last in the chain for that reason.
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

# How many commits one push is allowed to say out loud.
#
# The arithmetic behind the number rather than a round figure somebody liked: the worker gives a
# delivery sixty seconds, and this is ten GitHub reads plus ten Discord posts plus the sync that
# ran before it. That is survivable only because a batch cancelled halfway has already said
# everything it got through, and the claim it was holding when the deadline hit goes back.
#
# It is also about the channel. Eleven messages for one push is already the loudest thing this
# bot does, and a rebase of forty commits landing as forty messages would bury the conversation
# the thread exists for.
COMMITS_PER_PUSH = 10

PUSHED = "synchronize"

# What GitHub calls a head that is no longer a descendant of the base. `behind` is the one that
# is easy to leave out and the one that matters most: a `reset --hard HEAD~3 && push --force`
# leaves nothing ahead and `total_commits` at zero, so without it the thread says nothing at all
# about a push that threw three commits away.
REWRITTEN = frozenset({"behind", "diverged"})

# And the only status worth reading commits off. `identical` is a push that changed nothing the
# compare can see, which is what a no-op force push of the same tree looks like.
AHEAD = "ahead"

# A ref of forty zeros is git's way of saying there was nothing there. It cannot happen on a pull
# request, whose branch existed before the push by definition, but the field is read off a
# payload and is worth refusing rather than turning into a request for a compare against nothing.
_NOTHING = "0" * 40

Renderer = Callable[[Commit], Panel]
PushRenderer = Callable[[Actor | None], Panel]
CountRenderer = Callable[[int], Panel]


class CommitLine:
    """Posts a message per commit into a pull request's thread when somebody pushes.

    The same shape as `LabelLine` and `StateLine`: it reads the delivery, decides whether it has
    anything to say, and hands the words to `ClaimedLine`. What is different is where the words
    come from. A push payload carries the two ends of the range and nothing else, so everything
    a reader sees here is fetched.

    Keyed on the commit's SHA rather than on the delivery, which is the one design decision in
    this module worth arguing about. A label move is a fact about a delivery, so `LabelLine` keys
    on one; a commit is a fact about a SHA, and a single delivery carries up to ten of them. With
    a delivery key, a delivery that posts three of five and then fails would find the key claimed
    on its retry and post nothing, losing two commits for good while the delivery is recorded as
    handled. With a SHA key the retry is turned away on the three that landed and says the other
    two.

    What the key does not protect against: a rewritten commit is a new SHA and is genuinely a new
    commit, which is what the force-push line is for. And somebody merging a branch that carries
    their own earlier work announces it again in a different thread, because suppressing that
    needs a call per commit asking whether the default branch already has it, which triples the
    budget to hide something true.
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
        """Announce what this push did, in as few lines as tell the truth about it.

        Nothing is gathered up before anything is posted. Each commit is read, claimed and said
        before the next one is started, so a batch that runs out of time or hits a Discord failure
        has already delivered everything up to that point rather than losing the lot.
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
            # Once, and about the rewrite rather than about its commits. Every commit on a rebased
            # branch has a new SHA and would be announced as new work, so a rebase of five would
            # say five things nobody did just now. Keyed on the delivery because a force push is a
            # fact about one, and because two of them in a row are two separate things to say.
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
        # The newest, not the first. A long branch catching up puts other people's older commits
        # at the front of the range, and the ones somebody is waiting to review are at the end.
        wanted = theirs[-COMMITS_PER_PUSH:]

        said = 0
        for commit in wanted:
            if await self._say_one(arrival, repository.owner, repository.name, commit):
                said += 1

        await self._say_what_was_left(arrival, compared, kept=len(theirs), said=said)

    async def _say_one(self, arrival: Arrival, owner: str, name: str, ref: CommitRef) -> bool:
        """One commit, read and posted, or False for one GitHub could not be read.

        The numbers are a call each because the commit rows inside a compare carry no `stats`
        block at all. A commit that has gone between the compare and this read is skipped rather
        than raised: the branch was rewritten under us, and failing the whole delivery over it
        would lose the commits that are still there.
        """
        stats = await self._github.commit_stats(owner, name, ref.sha)
        if stats is None:
            logger.info("GitHub had no numbers for %s, so it is not announced", ref.sha)
            return False

        commit = Commit(sha=ref.sha, message=ref.message, author=ref.author, stats=stats)
        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            # Off the commit rather than written out here, which is where the two other kinds of
            # mirrored note already keep theirs. One place says what a key looks like, and the
            # reason it is the SHA is written down beside it.
            note_key=commit.note_key,
            panel=self._render(commit),
        )
        return True

    async def _say_what_was_left(
        self, arrival: Arrival, compared: CommitRange, *, kept: int, said: int
    ) -> None:
        """A footnote counting the commits this would have announced and did not.

        Counted from what was kept rather than from GitHub's total, which is the difference
        between a useful note and a lie about a merge: pulling main in brings forty commits with
        other people's names on them, none of which this was ever going to announce, and a
        footnote saying forty were left out would be the whole of what a merge posts.

        The unlisted rows are added back on top, because a compare stops at 250 commits while
        `total_commits` keeps counting. Nothing can be said about who wrote those, and they were
        certainly not announced.
        """
        unlisted = max(compared.total - len(compared.commits), 0)
        left = kept - said + unlisted
        if left <= 0:
            return

        # Deliberately not saying whether the cap, a merge or a failed read left them out. All of
        # them mean the same thing to whoever is reading, which is that GitHub has the rest.
        await self._line.say_once(
            tracked_item_id=arrival.tracked_item_id,
            thread_id=arrival.thread_id,
            note_key=f"commits-left:{arrival.arrived}",
            panel=self._left(left),
        )


def _ends(payload: JsonObject) -> tuple[str, str] | None:
    """The two ends of the push, or None for a payload that cannot say what moved."""
    before = payload.get("before")
    after = payload.get("after")
    for end in (before, after):
        if not isinstance(end, str) or not end or end == _NOTHING:
            return None
    return before, after  # type: ignore[return-value]


def _is_the_pushers_own_work(commit: CommitRef, pushed_by: Actor | None) -> bool:
    """Whether this commit is one the push is worth announcing.

    Two rules, and the second is narrower than "authored by whoever pushed" on purpose.

    A merge is dropped. It carries somebody else's whole branch behind it and says nothing about
    the work, so a pull request kept up to date with main would post one every time.

    A commit is dropped when it HAS a GitHub account and that account belongs to somebody else.
    Taken strictly, "not authored by the pusher" would also drop a commit whose account is null,
    which happens whenever somebody's git address is not registered to their profile; that would
    swallow their commits silently and they would have no way of guessing why. Merging main in
    still says nothing, because main's commits have real accounts that are not the pusher's.

    A push with no sender at all keeps everything. It cannot happen on a real webhook, and a
    guess about who pushed is a worse answer than announcing a commit twice.
    """
    if commit.merge:
        return False
    if commit.author is None or pushed_by is None:
        return True
    return commit.author.login.casefold() == pushed_by.login.casefold()
