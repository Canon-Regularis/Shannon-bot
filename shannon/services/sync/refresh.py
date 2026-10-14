"""Mirroring the open items that nothing ever opened a thread for, and redrawing the rest.

The bot learns about an issue or a pull request when a webhook arrives for it, so anything open
before the repository was registered, or open through a gap in delivery, has no thread and never
will. Nothing here pings: the sync services it is handed are built without a notifier, and with
`notifies` off, in `container._refresh`.

Issue #185. A refresh used to stop there, which made the name a promise it did not keep: a thread
whose block had gone stale stayed stale until somebody ran `/regenerate` in it, one at a time. So
every open pull request and issue that already has a thread is redrawn as well, from the snapshot
the listing has just read - a redraw asks GitHub nothing more. Closed items are not listed and are
left to `/regenerate`. Cards are left to the board poller, which redraws one every time it moves.

A draft card on a project board is the one kind no webhook EVER mentions. It exists only on the
board, so the only way to learn about one is to go and look, and until this covered them the only
thing that ever looked was the poller. That is the strongest reason tickets belong here: for the
other two kinds a refresh catches up on a gap, and for a ticket it is one of only two ways the
thing is ever seen at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.board import board_owner
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError, ShannonError
from shannon.domain.models import RepositorySnapshot, TrackedSnapshot
from shannon.github.client import ListsOpenItems
from shannon.github.errors import GitHubRateLimitError
from shannon.github.projects import UNREADABLE
from shannon.services.boards import BoardNotLinkedError, BoardUnreadableError
from shannon.services.sync.draft_cards import (
    ReadsBoards,
    forget_the_mirror,
    once_each,
    snapshot_of,
)
from shannon.services.sync.items import SyncOutcome, SyncResult, SyncsItems
from shannon.services.sync.manual import SyncFailedError

logger = logging.getLogger(__name__)

# How many items one run will mirror. Fixed by Discord, not by the deployment: a command has
# fifteen minutes after it defers and each item costs two Discord calls, the thread and the
# message in it, so a larger cap risks finishing the work after the token has expired and the
# reply can no longer be delivered. The redraws after them spend the same fifteen minutes, which
# is what `REDRAW_SECONDS` below is measured against.
MIRRORED_PER_RUN = 25

# How long one run spends redrawing threads that already exist, once its openings are done. Issue
# #185. A span of time rather than a count. An awake thread's redraw is two or three Discord calls
# and a quiet one's about six, so an ordinary repository's every open item fits in a fraction of
# this; a count small enough to be safe in the worst case - every item's lock held by a delivery
# for its whole fifteen seconds - would take an ordinary repository three runs instead of one.
# `test_a_refresh_fits_its_fifteen_minutes` holds that worst case inside the token.
REDRAW_SECONDS = 300.0


class RefreshScope(StrEnum):
    """Which kinds one run covers."""

    EVERYTHING = "everything"
    PULL_REQUESTS = "pull_requests"
    ISSUES = "issues"
    TICKETS = "tickets"


class MissedTickets(StrEnum):
    """Why a run covered no tickets, where that is worth saying out loud.

    Three members rather than a flag, because each sends somebody somewhere different: to
    `/board link`, to `/set_channel`, or to `/board show` and the log. `CardMove` settled the
    same question the same way, and said why: only some of the ways of declining are worth telling
    a person about.

    Absence is spelled `None` rather than a fourth member, so there is nothing to mean "covered"
    on a run that never asked about tickets at all.
    """

    NO_BOARD = "no board"
    NO_CHANNEL = "no channel"
    UNREADABLE = "unreadable"


# What each one raises when the person asked for tickets BY NAME. Nothing to decide at the call
# site, and a derived-set test can hold the table complete.
#
# The type and the words, not a built exception. Raising one instance over and over would hang a
# fresh `__traceback__` and `__context__` on a module-level object every time, which keeps the
# frames of every earlier refusal alive and puts the wrong one in the log.
_REFUSALS: dict[MissedTickets, tuple[type[ShannonError], str]] = {
    MissedTickets.NO_BOARD: (
        BoardNotLinkedError,
        "No board is linked to this server, so there are no tickets to mirror. "
        "Run /board link first.",
    ),
    MissedTickets.NO_CHANNEL: (
        SyncFailedError,
        "This server mirrors a board, but no channel is mapped for its tickets. "
        "Run /set_channel first.",
    ),
    MissedTickets.UNREADABLE: (
        BoardUnreadableError,
        "The board this server mirrors could not be read, so no tickets were covered. "
        "/board show says whose authorisation it is read with, and the log says what went "
        "wrong.",
    ),
}


@dataclass(frozen=True, slots=True)
class _Registered:
    """One read of the repositories row: the repository, its board, and where tickets go."""

    repository_id: int
    snapshot: RepositorySnapshot
    board_owner: str
    board_number: int | None
    ticket_channel: bool


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What a run did, in numbers, for the command to turn into a sentence.

    `failed` is counted inside `left`: an item this run could not mirror is still untracked, and
    a later run will try it again.

    `tickets_missed` is the one field that is not a number, and it is only ever set on a run that
    asked about tickets and could not reach them. `None` means there is nothing to add - including
    on `/refresh pull requests`, which never asks.

    `already` counts every listed item that had a thread, and since issue #185 each of them lands
    in exactly one of the five after it: redrawn, given a new thread because its own had gone, not
    redrawn because the attempt failed, not reached before the run's redraw time ran out, or a card
    left to the board.
    """

    full_name: str
    mirrored: int
    already: int
    failed: int
    left: int
    # Last, and defaulted, because the five above have no defaults and a dataclass cannot take a
    # defaulted field before them.
    tickets_missed: MissedTickets | None = None
    redrawn: int = 0
    rebuilt: int = 0
    redraws_failed: int = 0
    unreached: int = 0
    left_to_the_board: int = 0


@dataclass(frozen=True, slots=True)
class _Redraws:
    """What the redrawing half of a run came to."""

    redrawn: int = 0
    rebuilt: int = 0
    failed: int = 0
    unreached: int = 0


class RepositoryRefresh:
    """Mirror every open item on the registered repository that has no thread, and redraw the open
    pull requests and issues that have one."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: ListsOpenItems,
        boards: ReadsBoards,
        *,
        pull_requests: SyncsItems,
        issues: SyncsItems,
        tickets: SyncsItems,
        cap: int = MIRRORED_PER_RUN,
        redraw_seconds: float = REDRAW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        # A second reader, because a board is not reachable through the one above: it is a
        # different API on a different token, and widening `ListsOpenItems` would make every
        # GitHub client grow a board reader to satisfy the protocol.
        self._boards = boards
        self._syncs = {
            ObjectType.PR: pull_requests,
            ObjectType.ISSUE: issues,
            ObjectType.TICKET: tickets,
        }
        self._cap = cap
        self._redraw_seconds = redraw_seconds
        # Monotonic, as every span measured in this project is: a wall clock stepped back by an NTP
        # correction would hand a run its redraw time over again.
        self._clock = clock

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
        """Read the backlog, mirror what is missing from it, and redraw what is not."""
        registered = await self._registered(guild_id)
        current = await self._github.get_repository(
            registered.snapshot.owner, registered.snapshot.name
        )
        if current.github_repo_id != registered.snapshot.github_repo_id:
            raise RepositoryMismatchError(
                f"This server is registered to {registered.snapshot.full_name}, and GitHub now "
                "serves a different repository under that name. Somebody else has taken it, so "
                "nothing was mirrored."
            )

        # Every list before any of them is mirrored, even when the first exhausts the cap: the
        # extra reads buy an honest count of how many are still untracked. Tickets are listed
        # last, which is partly the cap - a capped run should spend itself on reviews - and
        # partly that the board is the slowest of the three reads.
        work: list[TrackedSnapshot] = []
        due: list[tuple[datetime, TrackedSnapshot]] = []
        already = 0
        left_to_the_board = 0
        missed: MissedTickets | None = None
        for object_type in _kinds(scope):
            if object_type is ObjectType.TICKET:
                found, missed = await self._drafts(registered)
                if missed is not None and scope is RefreshScope.TICKETS:
                    # Asked for by name, and the cause is something somebody can put right. A
                    # green panel reporting nothing would be worse than a refusal that says which
                    # command to run. Under EVERYTHING the same facts are a note instead, because
                    # the pull requests and issues were real work and saying "nothing was done"
                    # about them would be a lie.
                    refusal, words = _REFUSALS[missed]
                    raise refusal(words)
            else:
                found = await self._open_items(object_type, current)
            threaded = await self._threaded(
                repository_id=registered.repository_id, kind=object_type
            )
            for item in found:
                written = threaded.get(item.github_object_id)
                if written is None:
                    work.append(item)
                    continue
                already += 1
                if object_type is ObjectType.TICKET:
                    # The board poller redraws a card every time it moves, and asks the board for
                    # nothing to do it, so a refresh has nothing to add. Redrawing one here would
                    # also move the timestamp the poller decides by, and swallow a change it had
                    # yet to say anything about.
                    left_to_the_board += 1
                else:
                    due.append((written, item))

        # Openings first. An item with no thread is missing from the channel altogether, where one
        # with a stale block is at worst out of date, so a run short of time spends it on those.
        mirrored, failed = await self._mirror(
            work[: self._cap], repository_id=registered.repository_id
        )
        # Least recently written first, so the threads a run stops short of are the ones the next
        # run over the same kinds starts with: every redraw rewrites its row and goes to the back.
        # A thread a delivery has just rewritten goes to the back too, which is right, since it is
        # current already. Another scope sorts other rows in among them, so the reply says which.
        redraws = await self._redraw([item for _, item in sorted(due, key=lambda pair: pair[0])])
        return RefreshOutcome(
            full_name=current.full_name,
            mirrored=mirrored,
            already=already,
            failed=failed,
            left=len(work) - mirrored,
            tickets_missed=missed,
            redrawn=redraws.redrawn,
            rebuilt=redraws.rebuilt,
            redraws_failed=redraws.failed,
            unreached=redraws.unreached,
            left_to_the_board=left_to_the_board,
        )

    async def _registered(self, guild_id: int) -> _Registered:
        """Everything one read of the repositories row has to say, as plain values.

        Plain values rather than the row: everything after this happens outside the session, and a
        detached row is a lazy load waiting to fail.

        The board coordinates and the ticket channel come out of the SAME read as the repository.
        This used to return the repository alone, which read as tidy and was not: a second read to
        find the board would leave a window for /board link to land between the two, so a run could
        mirror against one board having decided against another. Two more scalars and a boolean
        cost nothing here and remove the question.
        """
        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
            if stored is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")
            owner, _, name = stored.repo_name.partition("/")
            mapped = await ChannelMappingStore(session).get(stored.id, ObjectType.TICKET)
            return _Registered(
                repository_id=stored.id,
                snapshot=RepositorySnapshot(
                    github_repo_id=stored.github_repo_id,
                    owner=owner,
                    name=name,
                    html_url=stored.repo_url,
                ),
                # A board can belong to a different owner than the repository. The fallback is
                # shared with every other caller - see `domain.board.board_owner`.
                board_owner=board_owner(
                    project_owner=stored.project_owner, repo_name=stored.repo_name
                ),
                board_number=stored.project_number,
                ticket_channel=mapped is not None,
            )

    async def _drafts(
        self, registered: _Registered
    ) -> tuple[Sequence[TrackedSnapshot], MissedTickets | None]:
        """The board's draft cards, or why there are none to offer.

        Only drafts. A card wrapping an issue or a pull request already has a thread from that
        item's own webhooks, and the two kinds above cover it.

        And only drafts still on the board. An archived card comes back from the read marked rather
        than dropped since issue #198, and offering one here would open a thread for work somebody
        put away.
        """
        if registered.board_number is None:
            return (), MissedTickets.NO_BOARD

        if not registered.ticket_channel:
            # Asked BEFORE the board is read, not after. A ticket has no fallback channel, so with
            # nothing mapped every card would answer NOT_TRACKED, and `_mirror` turns that into a
            # refusal that ends the whole run - after the pull requests and issues have already
            # been mirrored, so the person would be told nothing happened while twenty-five
            # threads had just appeared. Checking first also saves the slowest read of the three
            # when there is nowhere to put what it would return.
            return (), MissedTickets.NO_CHANNEL

        try:
            listed = await self._boards.list_board_items(
                registered.board_owner, registered.board_number
            )
        except (*UNREADABLE, GitHubRateLimitError) as unreadable:
            # Folded into one answer. An operator cannot act on the difference from a Discord
            # reply - the commonest cause is that nobody has authorised this bot to read that
            # board, or that the authorisation was withdrawn - and the log carries which it was.
            # The poller makes the same judgement about the same read, through the same tuple.
            logger.warning(
                "could not read board %s belonging to %r for a refresh, so no tickets were "
                "covered (%s)",
                registered.board_number,
                registered.board_owner,
                unreadable,
            )
            return (), MissedTickets.UNREADABLE

        return [
            snapshot_of(
                item,
                repository=registered.snapshot,
                project_number=registered.board_number,
                action="refreshed",
            )
            for item in once_each(listed)
            if item.is_draft and not item.archived
        ], None

    async def _open_items(
        self,
        object_type: Literal[ObjectType.PR, ObjectType.ISSUE],
        repository: RepositorySnapshot,
    ) -> Sequence[TrackedSnapshot]:
        """The open items of one kind, for the two kinds GitHub serves from the repository.

        Narrowed to those two in the SIGNATURE rather than guarded at runtime. The second arm is a
        catch-all, which used to mean a TICKET reaching here listed issues instead - silently, and
        counted as tickets. It cannot reach here now: `refresh` routes that kind to the board, and
        the type says so, so a fourth kind becomes a type error at the call site rather than wrong
        data in a thread.
        """
        if object_type is ObjectType.PR:
            return await self._github.list_open_pull_requests(repository)
        return await self._github.list_open_issues(repository)

    async def _threaded(self, *, repository_id: int, kind: ObjectType) -> dict[int, datetime]:
        """The items of one kind that already have a thread, and when each row was last written.

        Keyed on the thread, not the row: the row is committed before the Discord call that gives
        it one, so an item whose thread creation was refused is recorded here and invisible in
        the channel, and nothing but a webhook ever comes back for it. The write time is what the
        redraws are ordered by, issue #185.
        """
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).last_written(
                repository_id=repository_id, object_type=kind
            )

    async def _mirror(
        self, work: Sequence[TrackedSnapshot], *, repository_id: int
    ) -> tuple[int, int]:
        """One item at a time, counting what landed and what did not.

        Sequential rather than gathered: every sync holds a Postgres advisory lock, and holds the
        connection it took it on for the whole of its Discord phase, so running these at once
        would put the pool's fifteen connections against however many items the cap allows.
        """
        mirrored = 0
        failed = 0
        for snapshot in work:
            result = await self._attempt(snapshot, verb="mirror")
            if result is None:
                failed += 1
                await self._let_it_be_found_again(snapshot, repository_id=repository_id)
            elif result.synced:
                mirrored += 1
        return mirrored, failed

    async def _redraw(self, due: Sequence[TrackedSnapshot]) -> _Redraws:
        """Redraw open items' threads in the order given, until the run's redraw time is up.

        Issue #185. From the snapshot the listing already holds, through the same sync, so a
        redraw asks GitHub nothing and says nothing: no notifier, and real mentions Discord is told
        to ring none of. Sequential, for the reason `_mirror` gives.

        A thread somebody deleted comes back as a new one, which is counted apart because the
        reply says so. A superseded snapshot counts as redrawn: a delivery newer than the listing
        has written the block since, so the thread is as current as this would have made it.
        """
        began = self._clock()
        redrawn = rebuilt = failed = 0
        for taken, snapshot in enumerate(due):
            if self._clock() - began >= self._redraw_seconds:
                return _Redraws(redrawn, rebuilt, failed, unreached=len(due) - taken)
            result = await self._attempt(snapshot, verb="redraw")
            if result is None:
                failed += 1
            elif result.created:
                rebuilt += 1
            else:
                redrawn += 1
        return _Redraws(redrawn, rebuilt, failed)

    async def _attempt(self, snapshot: TrackedSnapshot, *, verb: str) -> SyncResult | None:
        """One item through its sync, or None where that failed - which is logged here, once.

        Shared by both halves of a run so the two cannot drift apart on what a failure is. A single
        thread Discord refuses must not take every item after it down, so a failure is counted
        rather than raised. Not having a channel is the exception: that is a refusal on the
        repository rather than on this item, and every item after it would be refused the same way.
        """
        try:
            result = await self._syncs[snapshot.object_type].sync(snapshot)
        except ShannonError as refusal:
            logger.warning(
                "could not %s %s#%s on a refresh: %s",
                verb,
                snapshot.repository.full_name,
                snapshot.number,
                refusal,
            )
            return None
        except Exception:
            # Letting an unexpected failure out would strand the command with no reply.
            logger.exception(
                "an unexpected failure trying to %s %s#%s on a refresh",
                verb,
                snapshot.repository.full_name,
                snapshot.number,
            )
            return None
        if result.outcome is SyncOutcome.NOT_TRACKED:
            raise SyncFailedError(
                "The repository is registered but has no channel mapped for that. "
                "Run /set_channel first."
            )
        return result

    async def _let_it_be_found_again(
        self, snapshot: TrackedSnapshot, *, repository_id: int
    ) -> None:
        """Undo the row a failed TICKET mirror leaves behind, so something comes back for it.

        Only tickets, and the asymmetry is the point. An issue or a pull request that fails here is
        found again by the next run on its own: the row is written before the Discord call, so a
        refused thread leaves `discord_thread_id` null, and `_threaded` keys on exactly that.

        A card can be left in a state neither guard sees. `ItemThreads` attaches the thread to the
        row BEFORE it re-raises, when Discord opened the thread and then refused the message in it,
        so the row ends up holding the card's timestamp AND a thread id. `_threaded` then counts it
        as already done, and the poller's `_has_moved` sees a timestamp that has not changed. The
        card is stranded behind an empty thread and nothing anywhere comes back for it.

        `to=None` rather than the value that was there, which is where this differs from the
        poller. The poller restores what was stored because its question is whether the card has
        moved since. This caller only ever reaches a card that had no thread, so the row as it was
        is a row the poller will skip; nothing is the only answer that always re-arms it.
        """
        if snapshot.object_type is not ObjectType.TICKET:
            return

        await forget_the_mirror(
            self._sessionmaker,
            repository_id=repository_id,
            card_id=snapshot.github_object_id,
            to=None,
        )


def _kinds(scope: RefreshScope) -> tuple[ObjectType, ...]:
    """Pull requests first, so a capped run spends it on reviews, not the issue backlog.

    Tickets last, for that reason and one more: the board is the slowest of the three reads, and
    the listing phase finishes before anything is mirrored.

    Every scope is matched BY NAME, including EVERYTHING. It used to be the fall-through, which
    meant a member added to `RefreshScope` without a branch here was silently treated as
    everything - no exception, nothing red, and a scope that quietly did four kinds. The arms are
    exhaustive now and the table is derived in a test, so a fifth member fails loudly.
    """
    if scope is RefreshScope.PULL_REQUESTS:
        return (ObjectType.PR,)
    if scope is RefreshScope.ISSUES:
        return (ObjectType.ISSUE,)
    if scope is RefreshScope.TICKETS:
        return (ObjectType.TICKET,)
    return (ObjectType.PR, ObjectType.ISSUE, ObjectType.TICKET)
