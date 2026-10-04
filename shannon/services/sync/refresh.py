"""Mirroring the open items that nothing ever opened a thread for.

The bot learns about an issue or a pull request when a webhook arrives for it, so anything open
before the repository was registered, or open through a gap in delivery, has no thread and never
will. Nothing here pings: the sync services it is handed are built without a notifier, in
`container._refresh`.

A draft card on a project board is the one kind no webhook EVER mentions. It exists only on the
board, so the only way to learn about one is to go and look, and until this covered them the only
thing that ever looked was the poller. That is the strongest reason tickets belong here: for the
other two kinds a refresh catches up on a gap, and for a ticket it is one of only two ways the
thing is ever seen at all.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
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
from shannon.services.sync.items import SyncOutcome, SyncsItems
from shannon.services.sync.manual import SyncFailedError

logger = logging.getLogger(__name__)

# How many items one run will mirror. Fixed by Discord, not by the deployment: a command has
# fifteen minutes after it defers and each item costs two Discord calls, the thread and the
# message in it, so a larger cap risks finishing the work after the token has expired and the
# reply can no longer be delivered.
MIRRORED_PER_RUN = 25


class RefreshScope(StrEnum):
    """Which kinds one run covers."""

    EVERYTHING = "everything"
    PULL_REQUESTS = "pull_requests"
    ISSUES = "issues"
    TICKETS = "tickets"


class MissedTickets(StrEnum):
    """Why a run covered no tickets, where that is worth saying out loud.

    Three members rather than a flag, because each sends somebody somewhere different: to
    `/set_board`, to `/set_channel`, or to the project token and the log. `CardMove` settled the
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
        "Run /set_board first.",
    ),
    MissedTickets.NO_CHANNEL: (
        SyncFailedError,
        "This server mirrors a board, but no channel is mapped for its tickets. "
        "Run /set_channel first.",
    ),
    MissedTickets.UNREADABLE: (
        BoardUnreadableError,
        "The board this server mirrors could not be read, so no tickets were covered. "
        "Nobody may have authorised this bot to read it, or the board moved; the log says "
        "what GitHub answered.",
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
    """

    full_name: str
    mirrored: int
    already: int
    failed: int
    left: int
    # Last, and defaulted, because the five above have no defaults and a dataclass cannot take a
    # defaulted field before them.
    tickets_missed: MissedTickets | None = None


class RepositoryRefresh:
    """Mirror every open item on the registered repository that has no thread."""

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

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
        """Read the backlog and mirror what is missing from it."""
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
        already = 0
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
                if item.github_object_id in threaded:
                    already += 1
                else:
                    work.append(item)

        mirrored, failed = await self._mirror(
            work[: self._cap], repository_id=registered.repository_id
        )
        return RefreshOutcome(
            full_name=current.full_name,
            mirrored=mirrored,
            already=already,
            failed=failed,
            left=len(work) - mirrored,
            tickets_missed=missed,
        )

    async def _registered(self, guild_id: int) -> _Registered:
        """Everything one read of the repositories row has to say, as plain values.

        Plain values rather than the row: everything after this happens outside the session, and a
        detached row is a lazy load waiting to fail.

        The board coordinates and the ticket channel come out of the SAME read as the repository.
        This used to return the repository alone, which read as tidy and was not: a second read to
        find the board would leave a window for /set_board to land between the two, so a run could
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
            if item.is_draft
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

    async def _threaded(self, *, repository_id: int, kind: ObjectType) -> set[int]:
        """The items of one kind that already have a thread, in one query.

        Keyed on the thread, not the row: the row is committed before the Discord call that gives
        it one, so an item whose thread creation was refused is recorded here and invisible in
        the channel, and nothing but a webhook ever comes back for it.
        """
        async with self._sessionmaker() as session:
            state = await TrackedItemStore(session).mirrored_state(
                repository_id=repository_id, object_type=kind
            )
        return {
            github_object_id
            for github_object_id, (_, thread_id) in state.items()
            if thread_id is not None
        }

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
            try:
                result = await self._syncs[snapshot.object_type].sync(snapshot)
            except ShannonError as refusal:
                # A single thread Discord refuses must not take every item after it down.
                failed += 1
                logger.warning(
                    "could not mirror %s#%s on a refresh: %s",
                    snapshot.repository.full_name,
                    snapshot.number,
                    refusal,
                )
                await self._let_it_be_found_again(snapshot, repository_id=repository_id)
            except Exception:
                # Letting an unexpected failure out would strand the command with no reply.
                failed += 1
                logger.exception(
                    "an unexpected failure mirroring %s#%s on a refresh",
                    snapshot.repository.full_name,
                    snapshot.number,
                )
                await self._let_it_be_found_again(snapshot, repository_id=repository_id)
            else:
                if result.outcome is SyncOutcome.NOT_TRACKED:
                    # A refusal on the repository or the channel, not on this item, so every
                    # item after it would be refused identically.
                    raise SyncFailedError(
                        "The repository is registered but has no channel mapped for that. "
                        "Run /set_channel first."
                    )
                if result.synced:
                    mirrored += 1
        return mirrored, failed

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
