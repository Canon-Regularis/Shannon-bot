from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.models import (
    Actor,
    ItemNote,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewSnapshot,
)
from shannon.services.audience import author_and_assignees, reachable
from shannon.services.locating import in_its_thread
from shannon.services.sync.announcements import Arrival, ClaimedLine
from shannon.services.sync.shutting import KeepsThreadsShut

logger = logging.getLogger(__name__)


class ReviewRequestLedger:
    """Closes a review request once the review it asked for has been submitted.

    GitHub drops a reviewer from `requested_reviewers` the moment they submit and sends no
    `pull_request` event saying so; the ping is driven by the assignment row existing, so without
    this the row survives with its `notified_at` set and re-request review reads as "already
    asked". Stamped rather than removed, because a delivery captured before the review and
    retried after it still lists the reviewer: a later payload is compared against the stamp, so
    a genuine re-request reopens it and a straggler does not.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def fulfilled(self, snapshot: ItemNote) -> None:
        if snapshot.author is None:
            return

        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                return

            item = await TrackedItemStore(session).get_by_number(
                repository_id=repository.id,
                number=snapshot.item_number,
                object_type=ObjectType.PR,
            )
            if item is None:
                return

            # Only the reviewer's own row. A team's request is closed by GitHub dropping it
            # from `requested_teams`, which deletes the row on the next delivery; stamping a team
            # row here makes it look answered, and `reopen_if_newer` then pings the role again on
            # the next event with a later timestamp, once per review round.
            cleared = await ItemAssignmentStore(session).mark_fulfilled(
                item.id,
                ActorRole.REVIEWER,
                snapshot.author.login,
                snapshot.created_at,
                snapshot.author.github_user_id,
            )

        if cleared:
            logger.info(
                "%s reviewed %s#%s, so their review request is closed",
                snapshot.author.login,
                snapshot.repository.full_name,
                snapshot.item_number,
            )


def is_worth_a_message(snapshot: ItemNote) -> bool:
    """Whether a submitted review says anything its inline comments do not.

    GitHub wraps every inline note in a review, so leaving notes or replying to somebody else's
    note submits a `commented` review with no body of its own, and mirroring that posts
    `**alice** left a review` with nothing underneath. For the other two verdicts the verdict is
    the content, so an approval with no body still posts.

    Asked by the mirror rather than by the parser: a review this declines still has to run the
    ledger that closes the request it answers, or the reviewer is pinged again for the review
    they just gave.
    """
    assert isinstance(snapshot, ReviewSnapshot)
    return snapshot.verdict != "commented" or bool(snapshot.body.strip())


# Skipped rather than counted. A COMMENTED review is a note and not a verdict — GitHub's own rule
# decides on the latest review per person that carries one — so a comment left after an approval
# must not clear it. PENDING is a review somebody started and has not submitted; GitHub shows a
# viewer only their own, so an App token ordinarily sees none.
#
# Named for what is SKIPPED rather than for what counts, so a state GitHub adds later is treated
# as a verdict and blocks the announcement. Silently quiet is a better failure than silently
# wrong: the first costs a message, and the second calls a pull request agreed when it is not.
_NOT_A_VERDICT = frozenset({"commented", "pending"})


def latest_verdicts(reviews: Sequence[ReviewSnapshot]) -> dict[str, str]:
    """What each person's review of a pull request currently says, keyed by lowered login.

    GitHub sends every review ever submitted, including the ones later replaced, so the answer is
    the last one each person left that carried a verdict at all.

    Sorted by id rather than trusted in the order it arrived. A review row is created when it is
    submitted and rewritten in place when it is dismissed, so id order is submission order — and
    sorting costs one call while removing a dependency on an ordering GitHub documents but does
    not promise, without adding an arm to cover.

    An account GitHub no longer has is skipped. It cannot be counted either way: the login is
    gone, so there is nobody to attribute the verdict to and nobody who could change it.
    """
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda review: review.review_id):
        if review.author is None:
            continue
        if review.verdict in _NOT_A_VERDICT:
            continue
        latest[review.author.login.lower()] = review.verdict
    return latest


class ReadsAPullRequestAndItsReviews(Protocol):
    """The two questions the approval round-up asks GitHub, and nothing else.

    Both are reads, so the service that can announce agreement holds no handle that could write a
    label or ask anybody for anything.
    """

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def list_reviews(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewSnapshot] | None: ...


class RendersApproval(Protocol):
    """How the round-up is worded, injected so this module holds no Discord vocabulary."""

    def __call__(
        self,
        approvals: int,
        *,
        people: Sequence[Actor],
        teams: Sequence[Actor],
        mentions: Mapping[str, int] | None,
        roles: Mapping[str, int] | None,
    ) -> Panel: ...


class EveryoneApprovedLine:
    """Says so in the thread once every review a pull request asked for has come back approving.

    Issue #155. Asked of GitHub rather than worked out from what has arrived, because nothing
    here can work it out: no verdict is stored, the rows recording who was asked are deleted as
    GitHub drops them, and a review is rewritten in place when it is dismissed — an action this
    bot does not subscribe to, so a tally kept here would go on counting an approval somebody had
    taken back.

    Run after the review is mirrored rather than before, so the round-up sits under the approval
    it counts rather than above a message nobody has seen yet.

    Not an announcer and not a notifier. `Arrival` wants a `TrackedSnapshot` and a review is not
    one; `item_assignments.notified_at` answers once for the life of a row, and a reviewer asked
    while the pull request was a draft has already spent theirs. The claim in `mirrored_notes` is
    what makes this say a thing once, the same bargain the CI announcer and the draft switch both
    struck for the same reason.

    No staleness guard, deliberately, and the absence is the design. The draft switch needs one
    because it renders from the delivery's own snapshot; everything here is read from GitHub at
    the moment of asking, so a retry landing after a push correctly evaluates the pull request as
    it is now and claims the head it actually read.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        github: ReadsAPullRequestAndItsReviews,
        render: RendersApproval,
        shut_again: KeepsThreadsShut,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._render = render
        self._line = ClaimedLine(sessionmaker, threads, shut_again=shut_again)

    async def after_a_review(self, snapshot: ItemNote) -> None:
        """One of the two moments the condition can become true: somebody approves.

        Hung off the review route after the note is mirrored, so the round-up sits under the
        approval it counts.
        """
        # Asserted rather than branched on: this is wired to the review route alone, and an arm
        # nothing can reach is one nothing can test either.
        assert isinstance(snapshot, ReviewSnapshot)

        # The gate, and the cheapest refusal there is. Most reviews on a busy repository are
        # comments, and this is what keeps the two GitHub calls below off all of them.
        #
        # Load-bearing rather than an optimisation. Without it, a bare comment on a pull request
        # nobody was asked to review reaches the test below with nothing to test, and every
        # verdict in an empty mapping is an approval — so the bot would announce agreement on a
        # pull request nobody had approved.
        if snapshot.verdict != "approved":
            return

        await self._round_up(snapshot.repository, snapshot.item_number, about="a review")

    async def say(self, arrival: Arrival) -> None:
        """The other moment, and the one a review can never deliver.

        Alice approves while Bob is still asked. Later the author withdraws Bob's request — and
        at that instant everybody remaining has approved, with no further review coming to notice
        it. Without this the thread simply never says so.

        The action and the type in one condition rather than two. One tuple of announcers serves
        the issues handler as well as the pull request one, so an issue delivery reaches this too
        — but an issue has no review requests to withdraw, so an action check that passed would
        already have proved the type, and a second `if` would be an arm nothing could reach.

        No claim of its own: this shares `approved:{head_sha}` with the review route, so whichever
        of the two gets there first is the one that speaks and the other finds it taken.
        """
        if arrival.action != "review_request_removed" or not isinstance(
            arrival.snapshot, PullRequestSnapshot
        ):
            return

        await self._round_up(
            arrival.snapshot.repository, arrival.snapshot.number, about="a withdrawn review request"
        )

    async def _round_up(self, repository: RepositorySnapshot, number: int, *, about: str) -> None:
        """Whether every review this pull request asked for has come back approving, and the
        saying so if it has.

        Everything below is read from GitHub at the moment of asking rather than taken from
        whatever delivery woke this, which is what lets two routes share it: neither one's payload
        has to be the truth, only its repository and its number.
        """
        async with self._sessionmaker() as session:
            found = await in_its_thread(
                session,
                repository=repository,
                number=number,
                object_type=ObjectType.PR,
                about=about,
            )
        if found is None:
            return

        item = await self._github.get_pull_request(repository.owner, repository.name, number)
        if item.draft or item.closed:
            # A draft has asked nobody yet, so there is no round to finish. A closed or merged
            # one is finished either way, and posting reopens an archived thread.
            return
        if not item.head_sha:
            # The claim would become the constant `approved:`, which is one claim for the whole
            # life of the item: it would announce once and then stay silent for every later
            # round, permanently, with nothing saying why.
            logger.warning(
                "%s#%s has no head commit, so its approvals are not announced",
                repository.full_name,
                number,
            )
            return
        if item.reviewers or item.reviewer_teams:
            # Somebody asked has not answered. GitHub empties these as people submit, so what is
            # left is the outstanding set rather than the roster.
            return

        reviews = await self._github.list_reviews(repository, number)
        if reviews is None:
            return
        latest = latest_verdicts(reviews)
        if not latest or any(verdict != "approved" for verdict in latest.values()):
            return

        async with self._sessionmaker() as session:
            people = author_and_assignees(item)
            audience = await reachable(session, guild_id=found.guild_id, people=people, teams=())

        await self._line.say_once(
            tracked_item_id=found.tracked_item_id,
            thread_id=found.thread_id,
            # The head this was evaluated against, not the commit the review named. The two
            # diverge whenever somebody approves a commit the branch has moved past, and keying
            # on the review's would announce twice for one agreed state and claim a head nobody
            # is on.
            note_key=f"approved:{item.head_sha}",
            panel=self._render(
                len(latest),
                people=people,
                teams=(),
                mentions=audience.mentions,
                roles=audience.roles,
            ),
            notify=audience.notify,
        )
