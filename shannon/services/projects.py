"""Mirroring a GitHub project board, by asking it rather than by being told.

Everything else in this bot is delivered: GitHub posts a webhook, the queue writes it down, the
worker acts on it. A project board cannot work that way. GitHub sends `projects_v2` webhooks for
organisation projects only, and never for a personal account's, so for the accounts this is most
likely to run against there is no event to receive at all. The events the requirements name,
`project_card.created` and its siblings, belong to Projects (classic), which GitHub sunset in
August 2024 and removed from Enterprise Server in 3.17.

So this polls. The cost is latency, bounded by the interval. The saving is that a personal board
and an organisation one work on the same code path, with no second webhook to install.

The board answers with every card every time, so the work is deciding which of them moved. That
is one query for what is stored and a comparison in memory, rather than a question per card.

The latency floor, and why it is where it is
--------------------------------------------
Issue #189, which opened because a ticket took thirty to forty-five seconds to reach Discord while
an issue was instant. Both go through the same `ItemSyncService.sync()`; the whole of the
difference was the clock. A webhook is queued and picked up by a worker polling every two seconds;
a board was read every sixty. A change lands uniformly inside that window, so the mean wait was
half of it - thirty seconds, which is exactly what was being reported.

There is no event to wait on, so the floor cannot be zero. It is:

    mean  =  interval / 2  +  one read  +  the sync
    worst =  interval      +  one read  +  the sync

At the interval this now ships with, that is a mean of roughly three seconds.

The interval is two seconds, and the reason it can be is one measured fact: **GitHub honours
`If-None-Match` on the project items endpoint, and a 304 carries no body AND spends no rate-limit
budget.** Ten conditional polls two seconds apart left `x-ratelimit-remaining` untouched. So a
board nobody has touched costs one request and nothing else, and thirty times the passes cost no
more per hour than sixty-second polling did. `Cache-Control: max-age=60` on that response is advice
to a client rather than a staleness floor - GitHub sends no `Age` and its `Date` advances on every
request - which is what makes a short interval mean anything at all.

`HttpProjectBoards` keeps the validator and the cards it describes, so an unchanged board answers
out of memory. That is sound rather than hopeful: a 304 proves the body was byte-identical, and
parsing is a pure function of the body. **Nothing in this module behaves differently for it.** The
cheap read is a transport saving and not a short circuit - every comparison, every retry and every
card with no thread yet is reached exactly as often as before - which is why the change could be
made without re-deriving what the poll is allowed to skip.

One board cannot be read that way: a validator hashes ONE response body, so a board too big for a
single page cannot be checked without downloading it. Such a board is polled on the slow clock
instead - see `UNVALIDATED_POLL_SECONDS` - because a megabyte every two seconds is the one way this
would cost more than it saves.

What would remove the floor: move the board to an organisation and subscribe to `projects_v2_item`.
The board then joins the same queue as everything else and the latency becomes the worker's, with
this module not needed at all. Two caveats, both already true elsewhere in the tree - those events
are organisation-scope and in public preview, and granting an installed App a new permission
suspends its deliveries until an admin accepts.

Following a card off the board
------------------------------
Issue #198. A draft card leaves the board in two ways and neither is an event: it is archived,
which somebody can undo, or deleted, which nobody can. So each pass also follows every mirrored
draft to wherever the board has put it - an archived card shuts its thread, a card put back opens
it again, and a deleted card ends it the way a conversion does. See `_settle_what_became_of`.

A card on the read answers for itself. A card missing from it proves nothing, because a read can
leave one out without saying so, so those are asked about one card at a time - the only request a
quiet board ever spends budget on, and spaced so that stays small. See `CARD_READ_SPACING_SECONDS`.

Things deliberately not done, so they are not re-litigated:

- **Per-card concurrency.** discord.py buckets thread creation on the parent channel, so the calls
  that cost would serialise anyway; at this interval the common case is one changed card, where
  concurrency buys nothing; and `ItemLock` pins a connection per sync, so going wide would mean
  raising `BACKGROUND_CONNECTIONS`. Measured, and the serial part was never the cost.
- **GraphQL.** It would cut a megabyte to a few kilobytes and lose on the axis that matters: a POST
  cannot be ETagged, so every poll would spend budget where a 304 spends none.
- **Waking the poller from a command.** It would couple the command table to this instance to save
  two seconds, once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import COLUMN_WIDTH, Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import BoardRow, TicketThread, TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.discord_bot.formatting import (
    as_timestamp,
    format_card_archived,
    format_card_changed,
    format_card_converted,
    format_card_deleted,
    format_card_restored,
)
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import KnowsItsServers, PostsToThread, ShutsThread
from shannon.domain.board import board_owner as owner_of_board
from shannon.domain.board import is_board_page, normalise, status_from_column
from shannon.domain.enums import CardState, ObjectType, Status
from shannon.domain.errors import PermanentError, ShannonError
from shannon.domain.json import JsonObject
from shannon.domain.models import RepositorySnapshot, TicketSnapshot
from shannon.domain.time import as_utc
from shannon.github.errors import GitHubAuthError, GitHubRateLimitError
from shannon.github.projects import UNREADABLE, BoardItem
from shannon.services.sync.draft_cards import (
    ReadsBoards,
    forget_the_mirror,
    once_each,
    snapshot_of,
)
from shannon.services.sync.items import SyncOutcome, SyncsItems
from shannon.services.workflow import WorkflowOutcome, WorkflowRefusedError

logger = logging.getLogger(__name__)

# The longest GitHub's own primary rate limit window runs, which resets hourly. A `retry-after`
# past that is a header nobody meant, and sitting one out would take the feature off for the rest
# of the day on the strength of a number nothing here can check.
RATE_LIMIT_CEILING = 3600

# How long to wait before reading a board that cannot be checked cheaply. Issue #189.
#
# The short interval is affordable because of one thing only: GitHub honours `If-None-Match` on
# the items endpoint, and a 304 to it carries no body and spends no rate-limit budget. A board
# that arrives in more than one page cannot be validated that way - an ETag hashes one response
# body, so page one's says nothing about page two - so every read of one is the whole board.
#
# At the fast interval that would be a megabyte every couple of seconds, for ever, which is the
# one way this change could cost more than it saves. So a board nobody can check cheaply keeps
# roughly the cadence it had before any of this: slower, which is the direction a surprise should
# always fail in. It is said once, at INFO, because a board quietly polling thirty times less
# often than its neighbour is not something anybody should have to infer.
#
# Deliberately not an environment knob. It is the cost of a megabyte rather than a preference,
# and an operator lowering it would be choosing a bandwidth bill they cannot see.
UNVALIDATED_POLL_SECONDS = 60.0

# Issue #198. How often the poller may ask GitHub about one card on its own, per board. A card the
# listing has stopped showing may be deleted, archived out of a listing that leaves archived cards
# out, or merely beyond what one read could prove it saw - and the only way to tell them apart is a
# request per card, against the linker's budget, which the listing spends none of while nothing
# changes. Ten seconds caps that at 360 an hour per board, a fraction of the linker's 5,000, and a
# board where nothing went missing spends none. Not an environment knob, for the reason the one
# above is not: it is the price of a question, not a preference.
CARD_READ_SPACING_SECONDS = 10.0
# How long a card GitHub says is still on the board, though the read left it out, waits before it
# is asked about again. A read missing it is the board's shape - one too big to arrive whole - and
# not news about the card, so asking every ten seconds would spend the budget on nothing.
MISSED_CARD_RECHECK_SECONDS = 600.0
# How long an archived card waits between questions, where the listing leaves archived cards out.
# It is asked at all only so a card deleted out of the archive is noticed, and its thread was shut
# when it was archived - so an hour's wait costs nothing anybody can see.
ARCHIVED_CARD_RECHECK_SECONDS = 3600.0


class SaysAndShuts(PostsToThread, ShutsThread, KnowsItsServers, Protocol):
    """Posting one line in a thread and shutting it, for a thread nothing will use again.

    The poller otherwise reaches Discord only through the sync service, which renders items
    rather than saying things. Handing a card over to the issue it became is the one moment
    it has something to say that is not an item, and a thread going silent for ever with no
    explanation is the failure being fixed.

    Composed from the Protocols that already say these, rather than restating them: a third
    copy of `post` would be a third thing to keep in step with the gateway. Whether the bot is
    in a server at all rides along since issue #198, because Discord refuses a bot that has been
    removed exactly as it refuses a missing permission, and only one of those is worth retrying.
    """


class MovesStatus(Protocol):
    """Setting a tracked item's status, which is what a card moving on a board amounts to.

    The same path a person takes with /status In review, deliberately. A board move and a command
    are the same event told two ways, and routing them differently is how the labels on GitHub
    and the block in Discord start disagreeing.
    """

    async def set_status(
        self, *, thread_id: int, status: Status, acting: int | None = None
    ) -> WorkflowOutcome: ...


class ReadsOneCard(Protocol):
    """Reading one card on its own, for a card the board's listing has stopped showing.

    Issue #198. Declared here, where it is consumed: `/refresh` reads a board once and asks nothing
    of the sort. None means GitHub has no such card on the board, and nothing else - see
    `HttpProjectBoards.read_card`, which keeps every other failure out of it.
    """

    async def read_card(
        self, owner: str, project_number: int, card_id: int
    ) -> BoardItem | None: ...


class ReadsBoardsAndCards(ReadsBoards, ReadsOneCard, Protocol):
    """Everything the poller asks GitHub about a board: its listing, and a card it leaves out."""


class ProjectPoller:
    """Reads a board on a timer and syncs the cards that have moved since the last read."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        projects: ReadsBoardsAndCards,
        sync: SyncsItems,
        workflow: MovesStatus,
        threads: SaysAndShuts,
        *,
        polling: bool = True,
        interval: float = 60.0,
        may_set_status: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._projects = projects
        self._sync = sync
        self._workflow = workflow
        self._threads = threads
        self._polling = polling
        self._interval = interval
        self._may_set_status = may_set_status
        # What the spacing of single-card reads is measured on. Monotonic, because a wall clock
        # stepped back by an NTP correction would hold every question back for as long as it moved.
        self._clock = clock
        # Issue #198, all three in memory: a restart costs a question or two, never a wrong answer.
        # When each tracked card was last asked about on its own, by tracked item id; the cards
        # GitHub has answered "no such card" for once, waiting on the second answer a deletion
        # needs; and when each board last had a card asked about, by (owner casefolded, number).
        self._asked: dict[int, float] = {}
        self._gone_once: set[int] = set()
        self._card_read_at: dict[tuple[str, int], float] = {}
        # Boards already said to be unreadable, so the line is loud once and then DEBUG. This
        # runs every couple of seconds since issue #189, and a board nobody's authorisation
        # stands behind - every board linked before issue #170, until somebody links it again -
        # used to put a warning in the log on every pass. Let go of once the board reads, so a
        # board that breaks again later is news again.
        self._unreadable: set[tuple[str, int]] = set()
        self._stopping = False
        self._stopped = asyncio.Event()
        # Whether every board the last pass read can be re-read for a conditional request. Set
        # per pass by `run_once`, so this initial value is only ever read by a poller that is
        # switched off - and True is the right answer there, because a pass that returns without
        # reading anything has nothing expensive in it to slow down for.
        self._cheap_to_recheck = True
        # The boards already named in the log as too big to check cheaply, so the line is said
        # once each rather than once a pass. Discarded again if a board shrinks back under a page.
        self._expensive: set[tuple[str, int]] = set()

    @property
    def enabled(self) -> bool:
        """Whether this process reads boards at all.

        It used to mean "a board is configured", read once from a number at boot - which made a
        board linked afterwards invisible until a restart, because the task itself was only
        created when this was true. It now means what the operator actually decides: whether
        THIS process is the one that polls. Which boards exist is a question for the database,
        asked afresh every pass.

        That moves where the multi-replica off switch lives. Setting the number to zero on every
        replica used to be what stopped two pollers racing on one card and undoing each other's
        moves; `SHANNON_POLL_BOARDS` is now that switch, and the number no longer is.
        """
        return self._polling

    @property
    def stopping(self) -> bool:
        """Whether this has been asked to stop, by a shutdown or by itself.

        Public because stopping is no longer only something done to this from outside: the guard
        below takes that decision on its own, so whether it did is a question worth being able to
        ask rather than a flag the loop alone reads.
        """
        return self._stopping

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()

    async def run_once(self) -> int:
        """Read every linked board and sync what moved, answering with how many cards that was.

        Boards are re-read from the database at the top of every pass rather than resolved once
        at boot, which is the whole of how `/board link` takes effect without a restart. A board
        linked at 12:00:05 is polled at 12:00:07, and the command's reply says so. Pushing a
        wake-up from the command instead would couple the command table to the poller instance
        to save two seconds, once - an argument that was thin when the interval was a minute and
        is now not an argument at all.
        """
        if not self.enabled:
            return 0

        # Reset per pass, and read by `run_forever` afterwards to pick the next wait. True until
        # a board says otherwise, so a deployment with nothing linked keeps the fast cadence and
        # pays one indexed query for it.
        self._cheap_to_recheck = True
        moved = 0
        for board in await self._boards():
            moved += await self._poll(board)
        return moved

    async def _poll(self, board: _Board) -> int:
        """One board: read it, and sync the cards that have moved since the last read."""
        key = (board.board_owner.casefold(), board.project_number)
        try:
            listed = await self._projects.list_board_items(board.board_owner, board.project_number)
        except UNREADABLE as unreadable:
            # Named rather than left to the loop's `logger.exception`, which answers a
            # misconfiguration with a traceback once a minute. Both answers are caught together
            # because an operator cannot act on the difference: GitHub hides a private board
            # from an authorisation that cannot see it behind 404 as readily as 403, so telling
            # the two apart in the message would be a confident guess rather than a diagnosis.
            #
            # Three things can be wrong and the log cannot tell which, so it names all three.
            # A board GitHub does not have, an owner it was asked under - the number is a
            # sequence kept per account, so the pair means something neither half does alone -
            # or a token that cannot see the board, which is the one most likely here.
            #
            # The third cause is the commonest and was the hardest to name while there was a
            # token to blame: since issue #170 a board is read under the authorisation of whoever
            # linked it, so "nobody authorised this" and "their authorisation no longer opens it"
            # both arrive here as a board that will not open. Both are fixed the same way, by
            # somebody linking it again - one command and one click since issue #201 - which is
            # what the line says.
            #
            # The repository is named because there can now be several boards, and a line saying
            # only that "board 3" failed is one an operator cannot act on when two servers each
            # have one.
            #
            # Loud once per board, then DEBUG, for the reason the fields warning gives: a line
            # that repeats every pass is one whoever reads the log learns to scroll past.
            level = logging.WARNING if key not in self._unreadable else logging.DEBUG
            self._unreadable.add(key)
            logger.log(
                level,
                "could not read board %s belonging to %r for %s, so there is nothing to mirror "
                "(%s). Run /board show in that server to see whose authorisation it is read "
                "with, and check the number against the board's URL and the owner against who "
                "owns it - the number is a sequence GitHub keeps per account, so the pair means "
                "something neither half does alone. If those are right, nobody has authorised "
                "this bot to read that board, or the authorisation it had has been withdrawn on "
                "GitHub: running /board link again and signing in puts it right",
                board.project_number,
                board.board_owner,
                board.snapshot.full_name,
                unreadable,
            )
            return 0
        self._unreadable.discard(key)

        # Asked after the read rather than before it, because the read is what decides the answer:
        # a board is only known cheap once one of its pages has come back unfull and validated.
        self._note_what_a_recheck_costs(board)

        items = once_each(listed)
        # The cards on the board itself. An archived card is read now rather than dropped (issue
        # #198), and nothing that mirrors or moves a card may see one: archiving is how work is
        # put away, and mirroring it would put back what somebody put away.
        active = [item for item in items if not item.archived]

        # Whether the board's Status field can be read at all, decided from the whole board
        # rather than from one card. A single card with no column is somebody clearing its
        # Status; every card with no column is the field itself gone, which is a shape nothing
        # below may believe. Only the board sees the difference.
        readable = any(_fits(item.column) for item in active)

        # Read HERE, beside the board, and deliberately not with the one below. `_move_tracked`
        # acts on a card having MOVED: it compares this listing against the column it last saw,
        # and that comparison is only sound if both halves were taken at the same moment.
        #
        # They were not. This used to be one read, taken after the drafts - and mirroring drafts
        # is Discord round trips, so seconds. A `/status` landing in that window wrote the card,
        # the row and `project_column`, and then the poll compared its own STALE listing against
        # that FRESH memory, read the difference as somebody dragging the card back, and wrote the
        # column it had read before the command ran straight over the top. With
        # `SHANNON_BOARD_MAY_SET_STATUS` on, the status went with it and the command was silently
        # undone; with it off the column alone was corrupted, which is enough - the rule that
        # refuses a skipped column measures from exactly that column.
        seen = await self._board_state(board.repository_id)

        # Issue #198, and here on purpose: after `seen`, so the window the comment above guards is
        # as narrow as it was, and before the drafts, so a card coming back says so before the
        # line saying what changed while it was away. Answers the cards whose sync must wait.
        owed = await self._settle_what_became_of(board, items)

        # Archived ones included, for the hand-over alone: a card converted to an issue is a
        # conversion wherever it sits, and its draft thread is owed the hand-over either way.
        wrapped = [i for i in items if not i.is_draft]
        moved = await self._mirror_drafts(
            board, [i for i in active if i.is_draft and i.item_id not in owed]
        )

        # Read again, and after the drafts on purpose: a draft mirrored this pass has a row only
        # now, and the hand-over is the thing that needs to see it. The two reads answer different
        # questions - `seen` is a snapshot to compare against, this is the current map - and one
        # read cannot be both without being wrong for one of them.
        state = await self._board_state(board.repository_id)
        await self._hand_over_converted(wrapped, state)
        moved += await self._move_tracked(
            board, [i for i in wrapped if not i.archived], readable, seen
        )

        if moved:
            logger.info("mirrored %s of %s cards that had moved", moved, len(active))
        return moved

    async def _settle_what_became_of(self, board: _Board, items: Sequence[BoardItem]) -> set[int]:
        """Follow every mirrored draft card to wherever the board has put it. Issue #198.

        An archived card shuts its thread, a card put back opens it again, and a deleted card ends
        it the way a conversion does - and none of the three is an event, so they are read off the
        board like everything else here.

        A card on this read answers for itself, and needs no proof of which board it is on: a card
        id is GitHub's, unique across every board there is. A card missing from the read proves
        nothing at all - a read past its page cap, a page GitHub garbled and cursor paging over a
        card being dragged all leave one out without saying so - so those are asked about on their
        own, one at a time, and only this board's. See `_ask_about_a_missing_card`.

        Answers the cards whose sync has to wait this pass: one coming back whose thread Discord
        would not reopen yet. Synced anyway, the sync would write `open` over `archived`, and the
        reopen would never be tried again over a card that is plainly back.
        """
        rows = await self._ticket_threads(board.repository_id)
        if not self._still_in(board.guild_id):
            # Out of the server, or not connected to it yet. Discord refuses the bot then exactly
            # as it refuses a missing permission, and that refusal would be written off for good
            # - so nothing is tried until it is back, and a card coming back is held out of its
            # sync meanwhile, which would otherwise write `open` over the reopen still owed.
            return {row.card_id for row in rows if row.archived}

        listed = {item.item_id: item for item in items}
        owed: set[int] = set()
        missing: list[TicketThread] = []
        for row in rows:
            card = listed.get(row.card_id)
            if card is None:
                if _on_this_board(board, row):
                    missing.append(row)
                continue
            # On the read, so whatever a question about it alone suspected is answered.
            self._asked.pop(row.tracked_item_id, None)
            self._gone_once.discard(row.tracked_item_id)
            try:
                await self._keep_its_page(board, row, card)
                if not await self._follow(row, card):
                    owed.add(row.card_id)
            except Exception:
                # One card's surprise is that card's, as in `_mirror_drafts`, and held back from
                # its sync as well: whatever went wrong may have left its row half way.
                logger.exception(
                    "could not follow card %s to where the board has put it", row.card_id
                )
                owed.add(row.card_id)
        await self._ask_about_a_missing_card(board, missing)
        return owed

    async def _follow(self, row: TicketThread, card: BoardItem) -> bool:
        """Do to a card's thread what the board has done to the card.

        Answers whether that is settled: false where Discord could not shut or reopen the thread
        this pass and nothing was written, so the caller brings the card round again - and holds a
        card coming back out of its sync meanwhile, see `_settle_what_became_of`.
        """
        fate = _fate_of(card, archived=row.archived)
        if fate is _Fate.ARCHIVED:
            return await self._archive(row)
        if fate is _Fate.RESTORED:
            return await self._restore(row)
        return True

    def _still_in(self, guild_id: int) -> bool:
        """Whether the bot is in a server now. Not connected counts as no: waiting suits both."""
        try:
            return self._threads.is_in(guild_id)
        except DiscordGatewayError:
            return False

    async def _archive(self, row: TicketThread) -> bool:
        """Shut a thread whose card was archived, and say so. Issue #198.

        Discord first and the row second, the opposite of a hand-over, because here the row is
        the retry. A shut Discord could not make for a moment writes nothing, so the next pass
        finds the card archived and the row open and asks again; written first, the row would
        say archived over a thread nobody shut, and nothing would ever look again. A shut Discord
        refuses outright is written off instead - asked every couple of seconds it would refuse
        every time - and said without a word about the lock it did not get.
        """
        shut = await self._try_to_shut(row.thread_id, shut=True)
        if shut is _Shut.LATER:
            return False
        if shut is _Shut.GONE:
            # Somebody deleted the thread. Nothing to shut or tell, and pointing at it would leave
            # the card with no thread when it comes back; let go, it comes back to a new one.
            await self._record(row, CardState.ARCHIVED, forget=True)
            return True
        done = shut is _Shut.DONE
        await self._record(row, CardState.ARCHIVED, locked=True if done else None)
        logger.info("card %s was archived, so thread %s was shut", row.card_id, row.thread_id)
        await self._say_and_shut_again(
            row.thread_id,
            format_card_archived(shut=done),
            shut=done,
            failing=f"tell thread {row.thread_id} its card was archived",
        )
        return True

    async def _restore(self, row: TicketThread) -> bool:
        """Reopen a thread whose card came back to the board, and say so. Issue #198.

        On the card's presence and the row's say-so, never on its timestamp: whether GitHub
        stamps a card it unarchives is not something this may assume, and a card nobody edited
        while it was away is no less back. Anything that did change is said by the ordinary line
        `_mirror_drafts` posts next, against the fields recorded before the card was archived -
        which is why an archived card is never synced.

        Discord first and the row second, as `_archive` explains. Answers whether the card's
        sync may go ahead: not while a reopen Discord could not make for a moment is still owed,
        because the sync writes `open` and would take the only record that it is.
        """
        reopened = await self._try_to_shut(row.thread_id, shut=False)
        if reopened is _Shut.LATER:
            return False
        if reopened is _Shut.GONE:
            # Let go of, so `_mirror_drafts` builds the card a new thread on this same pass.
            await self._record(row, CardState.OPEN, forget=True)
            return True
        done = reopened is _Shut.DONE
        await self._record(row, CardState.OPEN, locked=False if done else None)
        if not done:
            logger.warning(
                "card %s is back on the board and thread %s stays shut over it: the bot needs "
                "Manage Threads to reopen it, or somebody can unlock it by hand",
                row.card_id,
                row.thread_id,
            )
        await self._say_and_shut_again(
            row.thread_id,
            format_card_restored(reopened=done),
            shut=False,
            failing=f"tell thread {row.thread_id} its card is back on the board",
        )
        return True

    async def _gone(self, row: TicketThread) -> None:
        """A card GitHub says it does not have, believed the second time it says so. Issue #198.

        Twice, on two passes, because what follows cannot be undone - the thread is let go of and
        nothing will point at it again - while one 404 can be a board whose access changed between
        the read and this question, which the next pass then finds unreadable and asks nothing.
        """
        if row.tracked_item_id not in self._gone_once:
            self._gone_once.add(row.tracked_item_id)
            logger.info(
                "card %s is not on its board any more; asking once more before letting thread "
                "%s go",
                row.card_id,
                row.thread_id,
            )
            return
        self._gone_once.discard(row.tracked_item_id)
        await self._retire(row)

    async def _retire(self, row: TicketThread) -> None:
        """End a deleted card's thread the way a converted card's is ended. Issue #198.

        Let go of first and said second, exactly as the hand-over does and for its reason: a card
        deleted cannot come back, so the pointer is what stops this repeating, and a Discord
        refusal after it costs the line rather than a thread told it is finished on every pass.
        """
        await self._record(row, CardState.DELETED, forget=True)
        logger.info(
            "card %s was deleted from the board, so thread %s was let go of",
            row.card_id,
            row.thread_id,
        )
        shut = await self._shut_before_saying(
            row.thread_id, before=f"saying its card {row.card_id} was deleted"
        )
        await self._say_and_shut_again(
            row.thread_id,
            format_card_deleted(shut=shut),
            shut=shut,
            failing=f"tell thread {row.thread_id} its card was deleted",
        )

    async def _try_to_shut(self, thread_id: int, *, shut: bool) -> _Shut:
        """Shut or reopen a thread, answering what Discord made of it.

        Four answers rather than a bool, because the callers do four different things: a thread
        that is gone is let go of, a refusal that will never change is written off, and anything
        else is tried again next pass.
        """
        try:
            await self._threads.set_shut(thread_id=thread_id, shut=shut)
        except ThreadNotFoundError:
            return _Shut.GONE
        except DiscordGatewayError as refusal:
            logger.warning(
                "could not %s thread %s: %s", "shut" if shut else "reopen", thread_id, refusal
            )
            return _Shut.REFUSED if isinstance(refusal, PermanentError) else _Shut.LATER
        return _Shut.DONE

    async def _record(
        self,
        row: TicketThread,
        state: CardState,
        *,
        locked: bool | None = None,
        forget: bool = False,
    ) -> None:
        """Write where a card stands, and what became of its thread, in one transaction.

        The state first, because letting go of the pointer nulls the column every write here is
        guarded on. `locked` is only ever what Discord has just done, never what was asked for -
        None where it refused, which leaves the row saying nothing about a lock it never got.
        """
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_card_state(
                row.tracked_item_id, thread_id=row.thread_id, state=state
            )
            pointers = ThreadPointerStore(session)
            if forget:
                await pointers.forget_thread(row.tracked_item_id, dead_thread_id=row.thread_id)
            elif locked is not None:
                await pointers.note_the_lock(
                    row.tracked_item_id, thread_id=row.thread_id, locked=locked
                )

    async def _keep_its_page(self, board: _Board, row: TicketThread, card: BoardItem) -> None:
        """Keep a listed draft's board page current. Issue #198.

        The page is how a missing card is told to be this board's, and it names the board's
        owner: a row written before that account was renamed names an owner the board no longer
        has, so a card of its that later went missing would never be asked about. A card on the
        read proves its page. Written only where it differs and is this board's page at all, so
        a board nobody renamed costs a comparison and nothing else.
        """
        if card.html_url == row.board_url or not is_board_page(
            card.html_url, owner=board.board_owner, number=board.project_number
        ):
            return
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_board_page(
                row.tracked_item_id, page=card.html_url
            )

    async def _ask_about_a_missing_card(
        self, board: _Board, missing: Sequence[TicketThread]
    ) -> None:
        """Ask GitHub about one card the read left out, at most, and follow what it answers.

        One at a time and spaced, because each question is a request against the linker's budget
        where an unchanged listing costs none, and a board polled every couple of seconds would
        otherwise spend it on cards merely out of the read's reach. See the three waits at the top
        of this module.

        Which card first: one GitHub already answered "no such card" for once, so a deletion is
        confirmed rather than left half believed; then cards whose row says open, because one of
        those missing is the news; then archived ones, asked about only so one deleted out of the
        archive is noticed. Within each, the card asked about longest ago.

        A deletion needs two answers, see `_gone`. Anything else GitHub says is followed at once,
        and anything it cannot say this time is left for the wait to bring back round - except a
        rate limit, which ends the pass as it does for the listing.
        """
        key = (board.board_owner.casefold(), board.project_number)
        now = self._clock()
        last = self._card_read_at.get(key)
        if last is not None and now - last < CARD_READ_SPACING_SECONDS:
            return
        due = [row for row in missing if self._due(row, now)]
        if not due:
            return
        row = min(
            due,
            key=lambda one: (
                one.tracked_item_id not in self._gone_once,
                one.archived,
                self._asked.get(one.tracked_item_id, -math.inf),
            ),
        )
        # Recorded before the question, so one that fails waits its turn rather than being asked
        # again on every pass.
        self._card_read_at[key] = now
        self._asked[row.tracked_item_id] = now
        try:
            card = await self._projects.read_card(
                board.board_owner, board.project_number, row.card_id
            )
        except GitHubRateLimitError:
            raise
        except ShannonError as unanswered:
            # Never a deletion: only the card's own 404 is, and `read_card` keeps everything else
            # out of None. Asked again once its wait comes round.
            logger.warning(
                "could not ask GitHub about card %s, which board %s belonging to %r no longer "
                "lists: %s",
                row.card_id,
                board.project_number,
                board.board_owner,
                unanswered,
            )
            return
        try:
            if card is None:
                await self._gone(row)
            else:
                self._gone_once.discard(row.tracked_item_id)
                if not await self._follow(row, card):
                    # GitHub has said what became of the card and only Discord is behind, so it is
                    # due again at the board's next question rather than after its wait.
                    self._asked.pop(row.tracked_item_id, None)
        except Exception:
            logger.exception("could not follow card %s to where the board has put it", row.card_id)

    def _due(self, row: TicketThread, now: float) -> bool:
        """Whether a missing card's wait has run out. A first "no such card" has none: the
        second answer is what is waited for, and the spacing per board already paces it."""
        if row.tracked_item_id in self._gone_once:
            return True
        asked = self._asked.get(row.tracked_item_id)
        wait = ARCHIVED_CARD_RECHECK_SECONDS if row.archived else MISSED_CARD_RECHECK_SECONDS
        return asked is None or now - asked >= wait

    async def _mirror_drafts(self, board: _Board, drafts: Sequence[BoardItem]) -> int:
        """A draft card is its own item, so it gets a thread of its own.

        Decided on the card's timestamp, because the card is the thing: if GitHub says it has
        not been touched since the last read, nothing about it can have changed.
        """
        seen = await self._mirrored(board.repository_id)
        mirrored = 0
        for item in drafts:
            stored, thread_id = seen.get(item.item_id, (None, None))
            if not _has_moved(item, stored, thread_id):
                continue
            try:
                result = await self._sync.sync(self._snapshot(board, item))
            except ShannonError as error:
                # One card at a time, the way the wrapped half already does it. Without this a
                # single card Discord refuses takes the rest of the drafts with it and the
                # wrapped half after them, none of which had anything wrong.
                logger.warning("could not mirror the card %r: %s", item.title, error.message)
                await self._forget_the_mirror(board, item, stored)
                continue
            except Exception:
                # Anything the sync path did not expect, a card too wide for its column being
                # the one that got here first. Logged whole, because a surprise is a defect and
                # the traceback is what says where; caught, because `run_forever` swallows it
                # identically and takes every card after this one with it, on every poll, for
                # as long as the process lives.
                logger.exception("could not mirror the card %r", item.title)
                await self._forget_the_mirror(board, item, stored)
                continue

            # What the sync did, not merely that it returned. A repository with no channel
            # mapped for tickets answers with NOT_TRACKED on every card of every poll, and
            # counting those reports a board being mirrored while nothing is written.
            if result.synced:
                mirrored += 1
                # Issue #182. Always called, including for a card seen for the first time: the
                # recording is what the FIRST poll is for, and skipping it would leave the baseline
                # unwritten and slip every announcement by one poll. `thread_id` is the one from
                # before this sync, so None says the card had no thread a moment ago.
                await self._say_what_moved(board, item, thread_id)
                continue

            # The check stays explicit and the branch coverage floor is told not to look for
            # its other half: STALE is the only other outcome and it cannot happen here, because
            # it needs a stored timestamp newer than the card's and `_has_moved` above only lets
            # a card through when the card is the newer of the two.
            if result.outcome is SyncOutcome.NOT_TRACKED:  # pragma: no branch
                # Nothing about this card decided that: the sync refuses on the repository or on
                # the channel, so every remaining card would be refused the same way and each one
                # would open a session, run two queries and write the same warning. On a board of
                # any size that is the whole log, once a minute, for as long as nobody has run
                # /set_channel. One card is enough to learn it from.
                logger.warning(
                    "no channel is mapped for %s, so none of this board's %s cards can be "
                    "mirrored; run /set_channel",
                    ObjectType.TICKET.value,
                    len(drafts),
                )
                break
        return mirrored

    async def _forget_the_mirror(
        self, board: _Board, item: BoardItem, stored: datetime | None
    ) -> None:
        """Put the row back as it WAS, which is this caller's answer: the poller revisits a card
        whose timestamp has moved, so restoring the stored value is what re-arms it."""
        await forget_the_mirror(
            self._sessionmaker,
            repository_id=board.repository_id,
            card_id=item.item_id,
            to=stored,
        )

    async def _move_tracked(
        self,
        board: _Board,
        wrapped: Sequence[BoardItem],
        readable: bool,
        state: Mapping[tuple[ObjectType, int], BoardRow],
    ) -> int:
        """A card wrapping an issue or a pull request moves the thread that item already has.

        Not a second thread and not a second snapshot: the issue is mirrored from its own
        webhooks, and all the board adds is which column it sits in. That goes through the same
        path a person takes with /status In review, so the label on GitHub and the block in Discord
        cannot end up disagreeing about a status the board decided.

        Acting on a MOVE, not on a disagreement. Those are different questions and answering the
        wrong one is what made the board win every argument: a reviewer setting a pull request to
        ready for merge, on a card still sitting in `In Progress`, had the decision reverted
        within the interval, silently, because all the poller could see was that the two did not
        match. It compares against the column it last saw instead, so a card nobody has touched
        says nothing at all.
        """
        # One query for the whole board rather than one per card. Every card asks the same
        # question of the same table, and a board is read whole on every poll whether or not
        # anything moved.
        await self._remember_cards(wrapped, state)

        moved = 0
        for item in wrapped:
            try:
                moved += await self._move_one(item, state, readable)
            except GitHubRateLimitError:
                # The one failure that is about the pass rather than about the card. Waiting is
                # the only thing that helps and `run_forever` is where the waiting is done.
                raise
            except Exception:
                # The same bargain the draft half makes, for the same reason. Everything below
                # is per-card already; this is only about the failures nobody wrote a branch
                # for, which would otherwise end the poll and repeat for ever.
                logger.exception("could not move the card %r", item.title)
        return moved

    async def _move_one(
        self,
        item: BoardItem,
        state: Mapping[tuple[ObjectType, int], BoardRow],
        readable: bool,
    ) -> int:
        """Act on one card, answering with whether it moved anything."""
        if item.content_id is None:
            return 0

        tracked = state.get((item.kind, item.content_id))
        if tracked is None or tracked.thread_id is None:
            return 0

        column = _fits(item.column)
        if _same_column(column, tracked.column):
            if tracked.column is None:
                # Nothing moved and nothing was ever seen look the same here, and a card added
                # to a board carries no Status until somebody picks one, so this is what most
                # cards look like on the poll that first meets them. Passing over without
                # writing the column down leaves it null, null means never seen, and the
                # first-look guard below is still armed when a Status is finally set: the move
                # that sets it is read as a first look and dropped, and the column matches from
                # then on so no later poll revisits it.
                await self._remember_column(tracked, column, readable)
            return 0

        wanted = status_from_column(column)
        if wanted is None or (wanted is tracked.status and tracked.column is None):
            # A column nobody has taught us, or the first look at a card that already agrees
            # with its item. Neither is a move to carry out, and both have to be written down
            # or the same card is looked at again on every poll for ever.
            await self._remember_column(tracked, column, readable)
            return 0

        if tracked.column is None and tracked.status is not Status.NOT_REVIEWED:
            # First sight of this card. The board fills in an item nobody has said anything
            # about; it does not get to overwrite a decision somebody already made, because
            # from here the two are indistinguishable and only one of them was deliberate.
            logger.info(
                "leaving %s at %s: the board says %r but this is the first look at its card",
                item.title,
                tracked.status.value,
                column,
            )
            await self._remember_column(tracked, column, readable)
            return 0

        # A card that has moved before goes through even where the status already matches.
        # Setting a status is several steps and the stored one is written in the middle of them:
        # a card dragged to Done whose thread Discord then refused to lock comes back here with
        # the status already DONE and the lock still owed, and skipping on that reads the half
        # that succeeded as the whole. The column is the record of a move having been carried
        # through, and it says this one was not. Repeating a status nothing changed costs one
        # read of the item and writes nothing, which is what makes it safe to send round again.
        if not self._may_set_status:
            # The board is not allowed to decide this one. Moving an item is a project manager's
            # in Discord, and nothing GitHub sends with a board says who dragged the card, so a
            # board that could move items would be a way around that permission for anybody with
            # access to the board.
            #
            # Written down as seen all the same, because the column is what stops a card being
            # looked at again and this is a final answer rather than a bad moment. Said once per
            # move rather than once per poll, for the same reason.
            logger.info(
                "not moving %s to %s: the board is not allowed to set a status. Set "
                "SHANNON_BOARD_MAY_SET_STATUS to turn that on",
                item.title,
                wanted.value,
            )
            await self._remember_column(tracked, column, readable)
            return 0

        try:
            # Nobody is acting, which is both the mechanism and the truth. This runs BECAUSE
            # the card moved, so writing to the board would put back the column it has just
            # read - and since issue #170 a board write is made AS somebody, and no member asked
            # for this one. `acting=None` says both at once.
            moved = await self._workflow.set_status(
                thread_id=tracked.thread_id, status=wanted, acting=None
            )
        except WorkflowRefusedError as refusal:
            # A status the item cannot hold, such as anything but DONE on a closed issue.
            # The board is allowed to disagree with GitHub; it is not allowed to win. This is
            # a final answer rather than a bad moment, so the move is written off as seen and
            # the same complaint is not made again on every poll for ever.
            logger.info(
                "board column %r does not apply to %s: %s",
                column,
                item.title,
                refusal.message,
            )
            await self._remember_column(tracked, column, readable)
            return 0
        except GitHubRateLimitError:
            # Not this card's problem and not something to step over. GitHub is asking the whole
            # process to wait, and `run_forever` is the only thing that can: swallowed here, the
            # poller went back on its ordinary timer and kept asking, which lengthens a secondary
            # limit rather than waiting it out. Raised so it reaches that backoff, past the loop
            # above which otherwise keeps a card's failure from ending the pass.
            raise
        except (PermanentError, GitHubAuthError) as refusal:
            # A channel deleted, the bot removed from the server, a Discord permission taken
            # away, or a GitHub token that can still read and may no longer write. None of those
            # is a bad moment and none of them is waited out, so coming round again cannot help.
            #
            # A refused GitHub write is here rather than being permanent everywhere, because the
            # two callers want opposite things from it. A token being rotated is minutes long,
            # and a delivery has sixteen attempts over two hours precisely so it can sit out
            # something like that; making the error permanent would drop every delivery in the
            # window instead. The poller has no such budget: nothing else advances the column, so
            # a card it cannot move is asked about every minute for as long as the token is
            # wrong, and every card moved after it joins that set and never leaves. Each one
            # costs a GitHub read and a Discord call every minute, so a token narrowed to
            # read-only spends the hour's whole quota on cards it cannot move, and then nothing
            # that needs GitHub works either.
            #
            # That is the same unbounded retry the refused-lock branch below was written to
            # prevent. So the move is written off as seen and said once, and the board and the
            # thread disagree until somebody fixes what is wrong and moves the card again.
            logger.warning(
                "could not move %s to %s and no waiting will change that, so it will not be "
                "tried again: %s",
                item.title,
                wanted.value,
                refusal,
            )
            await self._remember_column(tracked, column, readable)
            return 0
        except ShannonError as error:
            # GitHub or Discord having a bad moment, which is not an answer about anything.
            # The column is deliberately NOT recorded: remembering it here would mark the
            # move as seen while it never happened, and since nothing else ever rederives a
            # status from a board, the card would sit in its new column for ever with the
            # old status and no poll would look at it again.
            logger.warning("could not move %s to %s: %s", item.title, wanted.value, error)
            return 0

        if moved.lock_refused and not moved.lock_refusal_is_permanent:
            # The same half-done move, reported rather than raised. A command answers a refused
            # lock by telling the person who ran it what did land and what did not, because they
            # are standing there and can act on it. Nobody is standing here, so the only way this
            # gets a second go is the card coming round again, and the column not being written
            # down is what sends it.
            logger.warning(
                "moved %s to %s but could not lock its thread; it will be tried again",
                item.title,
                wanted.value,
            )
            return 0

        if moved.lock_refused:
            # A permission is not a bad moment. Coming round again cannot help, and this card is
            # never going to stop coming: nothing else advances the column, so every card ever
            # dragged to Done joins a set that is retried on every poll and never leaves it,
            # costing a GitHub read and a Discord call each, once a minute, for as long as the
            # permission is missing. It grows with the team's throughput.
            #
            # So the move is written off as carried through, which is what the column means, and
            # said once. The thread stays open until somebody grants the permission and moves the
            # card again, which is the same bargain the rest of this file makes with a refusal
            # nothing can wait out.
            logger.warning(
                "moved %s to %s but Discord will not let this bot lock threads, so it stays "
                "open; grant Manage Threads and move the card again",
                item.title,
                wanted.value,
            )

        await self._remember_column(tracked, column, readable)
        return 1

    async def _remember_column(self, tracked: BoardRow, column: str, readable: bool) -> None:
        """Record where the card was, storing the empty string for a card with no column at all.

        Null has to keep meaning one thing, and it already means never seen. Writing null for a
        card whose Status somebody cleared would put it back to never seen, which re-arms the
        first-look guard and quietly drops the next real move.

        A card that had a column and now reads as having none is refused only when nothing else
        on the board has one either. Those are two different events that look identical from one
        card: somebody clearing that card's Status, and the board's whole Status field having
        gone unreadable. The second cannot be survived by believing it, because it would write
        the empty string over every remembered column at once and the poll after the field came
        back would read the whole board as having moved and drive all of it through the status
        commands, stripping whatever anybody had set by hand.

        The board is read whole, so the two are distinguishable after all, and this used to
        judge them one card at a time. Refusing to forget was said to cost nothing, on the
        grounds that the memory kept is a column the card has left and the next real move
        differs from it. That is untrue of the one move that goes back where it came from: the
        card returns to the column the stale memory names, reads as never having moved, and that
        move is dropped and never revisited.
        """
        if column == "" and tracked.column and not readable:
            logger.info(
                "not forgetting that card %s was in %r: nothing on this board has a column, "
                "which is what a Status field that cannot be read looks like",
                tracked.tracked_item_id,
                tracked.column,
            )
            return
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_column(tracked.tracked_item_id, column)

    async def _hand_over_converted(
        self,
        wrapped: Sequence[BoardItem],
        state: Mapping[tuple[ObjectType, int], BoardRow],
    ) -> None:
        """Let go of the thread of a draft somebody converted into an issue.

        Clicking Convert to issue on GitHub keeps the card and its project item id and flips
        what it wraps. So the card leaves the draft half of this poll and enters the tracked
        half, which finds items by content id - and the ticket row, keyed by the CARD id,
        is never visited again. Its thread was frozen at the last draft render while the
        issue opened a second one from its own webhook: two threads, one piece of work, and
        nothing anywhere saying so.

        Detected for free. `board_state` filters by no type, so a ticket row is already in
        the map under its card id and this costs one lookup per wrapped card.

        The row is kept rather than deleted. It is the idempotency guard: the card is
        offered again on every poll for ever, and a null pointer is what makes the second
        visit do nothing. Deleted, nothing would record that the hand-over happened.

        Forget first, say second - the same order `ThreadRelocation` uses, so a Discord
        refusal costs the line rather than leaving a pointer at a thread nobody will read.
        """
        for item in wrapped:
            ghost = state.get((ObjectType.TICKET, item.item_id))
            if ghost is None or ghost.thread_id is None:
                continue

            async with self._sessionmaker() as session, session.begin():
                await ThreadPointerStore(session).forget_thread(
                    ghost.tracked_item_id, dead_thread_id=ghost.thread_id
                )
            logger.info(
                "card %s became %s, so its draft thread was handed over",
                item.item_id,
                item.html_url,
            )
            await self._say_it_moved(ghost.thread_id, item.html_url)

    async def _say_what_moved(self, board: _Board, item: BoardItem, thread_id: int | None) -> None:
        """Say what changed on a card, in one line, and write down what a reader has now seen.

        Issue #182. Record FIRST and say second, which is the order `_hand_over_converted` uses and
        for the same reason: a poller is a loop rather than a queue, so a Discord refusal after the
        write costs this one announcement, where a refusal before it would say the same thing on
        every poll for as long as the permission was missing.

        A card whose fields have never been recorded says nothing at all. That is every card on
        every board the minute this ships, and without the rule each of them would announce every
        field it has at once - which is the loudest possible way to deploy a quiet feature. It is
        also how a card mirrored before this column existed catches up: recorded once, silent once,
        and ordinary from then on.

        Nothing is claimed in `mirrored_notes`. The stored values ARE the guard: a second poll
        comparing a card against what it just wrote finds nothing moved and says nothing, which is
        the same work the claim would do and one query instead of two.
        """
        now = _board_fields_of(item)
        before = await self._shown_fields(board.repository_id, item.item_id)
        # Recorded first and unconditionally, which is what makes a first poll silent rather than
        # unrecorded: the row exists by now because the sync above just wrote it, and its
        # `shown_fields` is null until this write.
        await self._remember_shown_fields(board.repository_id, item.item_id, now)

        # Nothing to compare against, or nowhere to say it. The second is not redundant: the
        # conversion hand-over keeps a card's row and lets go of its thread, so a card can have
        # recorded fields and no thread to tell.
        if before is None or thread_id is None:
            return

        moved = _what_moved(before, now)
        if not moved:
            return

        try:
            await self._threads.post(thread_id=thread_id, panel=format_card_changed(moved))
        except DiscordGatewayError as refusal:
            # Swallowed, and the record already written. The alternative is saying it again every
            # minute until somebody grants Manage Messages, which is worse than missing it once.
            logger.warning("could not say what moved on the card %r: %s", item.title, refusal)

    async def _shown_fields(self, repository_id: int, card_id: int) -> JsonObject | None:
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).shown_fields(
                repository_id=repository_id, card_id=card_id
            )

    async def _remember_shown_fields(
        self, repository_id: int, card_id: int, fields: JsonObject
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_shown_fields(
                repository_id=repository_id, card_id=card_id, fields=fields
            )

    async def _say_it_moved(self, thread_id: int, html_url: str) -> None:
        """Point the old thread at the issue, and shut it.

        Shut FIRST, then post, then shut again - the opposite order to the relocation path this
        used to copy, and deliberately. `ThreadRelocation` says its signpost goes in before the
        shut because posting reopens an archived thread; the cost of that order is that the line
        cannot say whether the thread ended up locked, so its lines do not claim it. This one is
        an end state and does claim it (issue #184), so it has to know, and the only way to know
        is to have already asked. The second shut is what puts back what the post reopened, the
        way every write path in this project does.

        Swallowed on a refusal, as before. The pointer is already gone by the time Discord is
        asked, so what a refusal costs is never the hand-over, which has happened. It costs less
        than it used to as well, and that is the one behaviour this reordering changes: a refused
        shut costs the lock and a refused post costs the signpost, where posting first meant a
        refused post took the lock down with it and left the thread open with nothing said in it.

        One handler around the post and the second shut rather than one each. A third `except`
        could not be reached by any test - a failure of the second shut needs the first to have
        succeeded, and neither `fail_next_shut` nor `refuses_every_shut` can say that - so its
        log line would be an unreachable statement under the coverage floor. Nothing may escape
        either: this runs before the cards that have moved are moved, so an exception here would
        cost every one of them for the whole pass.
        """
        shut = await self._shut_before_saying(thread_id, before=f"handing it over to {html_url}")
        await self._say_and_shut_again(
            thread_id,
            format_card_converted(html_url, shut=shut),
            shut=shut,
            failing=f"hand thread {thread_id} over to {html_url}",
        )

    async def _shut_before_saying(self, thread_id: int, *, before: str) -> bool:
        """Shut a thread about to be told it is finished, answering whether Discord did.

        The first half of `_say_it_moved`'s order, shared with a deleted card's thread (issue
        #198), which is finished the same way and says the same nothing about a lock it could not
        take. `before` finishes the log line's sentence.
        """
        try:
            await self._threads.set_shut(thread_id=thread_id, shut=True)
        except DiscordGatewayError as refusal:
            logger.warning("could not shut thread %s before %s: %s", thread_id, before, refusal)
            return False
        return True

    async def _say_and_shut_again(
        self, thread_id: int, panel: Panel, *, shut: bool, failing: str
    ) -> None:
        """Post one line in a thread, and shut it again where it was shut: posting reopens it.

        The second half of `_say_it_moved`'s order, one handler around both for the reason its
        docstring gives, shared by every line the poller leaves in a thread it has just shut or
        reopened. `failing` finishes the sentence "could not ..." for the log.
        """
        try:
            await self._threads.post(thread_id=thread_id, panel=panel)
            if shut:
                await self._threads.set_shut(thread_id=thread_id, shut=True)
        except DiscordGatewayError as refusal:
            logger.warning("could not %s: %s", failing, refusal)

    async def _remember_cards(
        self, wrapped: Sequence[BoardItem], state: Mapping[tuple[ObjectType, int], BoardRow]
    ) -> None:
        """Write down which card wraps each item, for anything that wants to move one.

        Here rather than inside the per-card loop, and that is the whole of why it exists as
        its own pass. A card that has not moved returns early below without writing anything,
        and on an ordinary board that is nearly every card on nearly every poll - so a board
        that has been stable since the deploy would never record a single id, and a `/status`
        on any item on it would have nothing to write to.

        The pairing exists nowhere else. REST answers no per-item project lookup, so reading
        a board whole and inverting it is the only way to learn it, and this is the only
        place that does. Decided in memory and written only where it differs, so it costs one
        statement per card the first time a board is seen and nothing on every poll after.
        """
        pairs = {
            tracked.tracked_item_id: item.item_id
            for item in wrapped
            if item.content_id is not None
            and (tracked := state.get((item.kind, item.content_id))) is not None
            and tracked.card_id != item.item_id
        }
        if not pairs:
            return

        async with self._sessionmaker() as session, session.begin():
            await TrackedItemStore(session).remember_cards(pairs)

    async def _board_state(self, repository_id: int) -> Mapping[tuple[ObjectType, int], BoardRow]:
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).board_state(repository_id=repository_id)

    async def _ticket_threads(self, repository_id: int) -> list[TicketThread]:
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).ticket_threads(repository_id=repository_id)

    async def run_forever(self) -> None:
        """Read the board until asked to stop.

        A failure is logged and waited out rather than ending the loop, for the reason the
        delivery worker does the same: a board that cannot be read this minute is usually
        readable the next, and a poller that dies takes the feature with it until a restart.
        """
        while not self._stopping:
            wait = self._interval
            try:
                await self.run_once()
                # After the pass, because only the pass knows. A board that cannot be checked with
                # a conditional request costs its whole body every read, and reading a megabyte
                # every couple of seconds is the one way this interval could cost more than it
                # saves. See `UNVALIDATED_POLL_SECONDS`.
                wait = max(wait, self._wait_for_an_expensive_board())
            except asyncio.CancelledError:
                raise
            except GitHubRateLimitError as limit:
                # GitHub answers a spent limit with the moment the window reopens, and reading
                # the board again inside that window cannot succeed. On the interval alone this
                # is a full board read a minute for as long as the limit lasts, which is worse
                # than wasted: GitHub lengthens a secondary limit for requests made during one.
                # The longer of the two, so a header asking for no wait cannot talk the poller
                # out of its own interval.
                wait = max(wait, min(limit.retry_after or 0, RATE_LIMIT_CEILING))
                logger.warning(
                    "GitHub's rate limit is spent, waiting %ss before reading the board again",
                    int(wait),
                )
            except Exception:
                logger.exception("could not read the project board, carrying on")
            await self._wait(wait)

    def _note_what_a_recheck_costs(self, board: _Board) -> None:
        """Record whether this board can be read again for a conditional request.

        Said once per board rather than once per pass: at the fast interval a line per pass is
        eighteen hundred an hour, and the thing worth knowing - that this board polls on a slower
        clock than the rest - does not change between passes.
        """
        key = (board.board_owner, board.project_number)
        if self._projects.can_recheck_cheaply(*key):
            self._expensive.discard(key)
            return

        self._cheap_to_recheck = False
        if key not in self._expensive:
            self._expensive.add(key)
            logger.info(
                "board %s belonging to %r does not fit in one page, so it cannot be checked "
                "without reading the whole of it; polling it every %ss instead of every %ss",
                board.project_number,
                board.board_owner,
                int(UNVALIDATED_POLL_SECONDS),
                self._interval,
            )

    def _wait_for_an_expensive_board(self) -> float:
        """The floor the last pass earned: nothing, or the slow clock if a board needs it.

        A one-line conditional rather than an `if`. Coverage records arcs between line numbers,
        so written as a branch this would be two arms to reach where the sentence is the same
        either way.
        """
        return 0.0 if self._cheap_to_recheck else UNVALIDATED_POLL_SECONDS

    async def _wait(self, seconds: float) -> None:
        """Sleep, or wake at once if a stop arrives.

        Waiting on the sleep alone would leave a shutdown sitting out the whole interval, and at
        a minute that is far longer than the grace period allows.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)

    async def _boards(self) -> Sequence[_Board]:
        """Every board to read this pass, newest state of the database each time.

        A board is one a repository has linked, and nothing else. There used to be a second arm,
        reading a board named in the environment where no repository had linked one; since issue
        #170 a board is read under its linker's authorisation, and one named in the environment
        had no linker, so that arm could only ever return a board that would not open.

        The refusal it replaced stopped the poller outright with more than one server
        registered, and said to set the number to zero or give that server a deployment of its
        own. That was never a guard against a hard problem - it was the shape of a missing
        column. Nothing elected which repository a board belonged to because nothing recorded it.

        Built inside the session on purpose. The old one got away with building after it closed
        because every attribute was already loaded; a row read after an expire raises a lazy load
        inside the poll loop, where `run_forever` swallows it and it surfaces as a board that
        silently stopped.
        """
        async with self._sessionmaker() as session:
            linked = await RepositoryStore(session).with_boards()
            return [
                _Board.of(row, number)
                for row in linked
                # Narrowed per row rather than trusted from the WHERE clause, which filters in SQL
                # and tells the type checker nothing.
                if (number := row.project_number) is not None
            ]

    async def _mirrored(self, repository_id: int) -> dict[int, tuple[datetime | None, int | None]]:
        async with self._sessionmaker() as session:
            return await TrackedItemStore(session).mirrored_state(
                repository_id=repository_id, object_type=ObjectType.TICKET
            )

    def _snapshot(self, board: _Board, item: BoardItem) -> TicketSnapshot:
        """The card as the sync path sees it. `polled` is this caller's name for why it looked."""
        return snapshot_of(
            item,
            repository=board.snapshot,
            project_number=board.project_number,
            action="polled",
        )


# What a change line reports, as {stored key: the label a reader sees}, in the order the block
# puts them. Issue #182.
#
# Creator is the only field left out, and the issue asked for that: a card's creator does not
# change, and being told who made something is not news about it moving.
#
# `Updated` is in, and `Ticket Name` and `Description` are in BECAUSE it is. The poll only looks at
# a card whose timestamp moved, so `Updated` differs every single time this runs - which on its own
# would post a line saying a card changed without saying what. The title and the description are
# the two things that move a timestamp without moving anything else here, so watching them is what
# gives that line a cause to name. A bare timestamp line is the rare case rather than the common
# one now, and it means what it says: GitHub re-stamped a card and nothing a reader can see moved
# with it.
#
# `Created` can never differ in practice. It is here because the field list asked for it and
# because a row that silently ignored one of them would be the kind of gap nobody finds.
_WATCHED: dict[str, str] = {
    "title": "Ticket Name",
    "assignees": "Assignees",
    "status": "Status",
    "priority": "Priority",
    "story_point": "Story Point",
    "iteration": "Iteration",
    "area": "Area",
    "labels": "Tags",
    "created": "Created",
    "updated": "Updated",
    "body": "Description",
}


def _board_fields_of(item: BoardItem) -> JsonObject:
    """A card's watched fields, as the text a reader was shown.

    Stored as text rather than as ids, which is what makes the comparison and the block agree: the
    row holds what the thread said, so a renamed option reads as a change because to a reader it
    IS one - the thread said `HIGH` yesterday and says `URGENT` today.

    The two lists are joined rather than kept as arrays. A change line names them in one phrase
    either way, and ordering is GitHub's to decide, so a join keeps the stored shape flat and the
    comparison a string compare.
    """
    return {
        "title": item.title,
        "assignees": ", ".join(person.login for person in item.assignees),
        "status": item.column or "",
        "priority": item.priority_name or "",
        "story_point": item.story_point or "",
        "iteration": item.iteration or "",
        "area": item.area or "",
        "labels": ", ".join(label.name for label in item.labels),
        # Through the renderer the block uses rather than a second copy of Discord's timestamp
        # syntax: what a change line says a field moved to has to be what the row above it shows.
        "created": as_timestamp(item.created_at) if item.created_at else "",
        "updated": as_timestamp(item.updated_at) if item.updated_at else "",
        "body": item.body,
    }


def _what_moved(before: JsonObject, now: JsonObject) -> list[tuple[str, str, str]]:
    """Every watched field whose text differs, in the order a reader scans the block.

    A key missing from `before` is read as empty rather than skipped, which is what a card
    recorded before a board gained a field looks like: the field appearing IS the change, and
    "None to General" is what setting it did.
    """
    moved: list[tuple[str, str, str]] = []
    for key, label in _WATCHED.items():
        was = before.get(key)
        is_now = now.get(key)
        said = was if isinstance(was, str) else ""
        says = is_now if isinstance(is_now, str) else ""
        if said != says:
            moved.append((label, said, says))
    return moved


def _fits(column: str | None) -> str:
    """The card's column, cut to what the row will hold, and never null.

    A board's Status is whatever somebody typed into a field named Status, and the field does
    not have to be a single select at all: the poller matches it by name. A value wider than the
    row raises out of the flush, past the per-card handling, and stalls the board behind that
    one card. Cutting here rather than at the write is what keeps the comparison honest, since
    what is compared next poll is what was stored.
    """
    return (column or "")[:COLUMN_WIDTH]


def _same_column(seen: str | None, remembered: str | None) -> bool:
    """Whether a card is where the last poll left it.

    Compared the way the column is read: trimmed and case-folded, so a board renaming `Done` to
    `done` is not a move and does not restate a status nobody changed.
    """
    return normalise(seen or "") == normalise(remembered or "")


def _has_moved(item: BoardItem, stored: datetime | None, thread_id: int | None) -> bool:
    """Whether a card is worth syncing.

    Strictly newer, because equal means untouched since the last read. The sync path treats
    equal timestamps as current on purpose, which is right for a delivery that may be a retry
    and wrong for a poll that sees the same card every minute.

    A card with no thread is always synced, whatever its timestamp says. The row is written and
    committed before the Discord call that opens the thread, so a card can be recorded as
    current and have nothing to show for it, and comparing timestamps alone would leave it that
    way until somebody happened to touch it on GitHub. Nothing else rescues a draft: an issue
    gets another webhook, a draft has only this.

    A card with no timestamp is always synced too. GitHub gives one, so that is a guard rather
    than a case, and syncing too often is a wasted edit where skipping is a change nobody sees.
    """
    if thread_id is None or item.updated_at is None or stored is None:
        return True
    return as_utc(item.updated_at) > as_utc(stored)


class _Fate(StrEnum):
    """What the board has done to a mirrored draft card since its row was last written."""

    ARCHIVED = "archived"
    RESTORED = "restored"


class _Shut(StrEnum):
    """What Discord made of a request to shut or reopen a thread. See `_try_to_shut`."""

    DONE = "done"
    GONE = "gone"
    REFUSED = "refused"
    LATER = "later"


def _fate_of(card: BoardItem, *, archived: bool) -> _Fate | None:
    """What has become of a card whose row says `archived`, or None where nothing has.

    A card that is no longer a draft is nobody's business here: it was converted, and the
    hand-over is what follows that, archived or not.
    """
    if not card.is_draft or card.archived is archived:
        return None
    return _Fate.ARCHIVED if card.archived else _Fate.RESTORED


def _on_this_board(board: _Board, row: TicketThread) -> bool:
    """Whether a ticket row belongs to the board this pass read, owner and number both.

    Only asked of a card the read left out. A relink leaves the old board's rows behind, and
    asked about on the new board every one of their cards answers that GitHub has no such card
    - which would be read as deleted. The page names the board's number as well as its owner, so
    it is the whole answer. See `domain.board.is_board_page`.
    """
    return is_board_page(row.board_url, owner=board.board_owner, number=board.project_number)


@dataclass(frozen=True, slots=True)
class _Board:
    """The registered repository and the board read against it, as plain values.

    Two owners, kept apart on purpose. `board_owner` addresses the board; the snapshot's owner
    names the repository every mirrored card is filed under. They are the same account often
    enough to invite one field, and the cost of that was not a misleading log line: the sync
    handed each snapshot's full name to `follow_rename`, which wrote it to the `repositories`
    row. One poll renamed the registered repository to the board owner's, and `of` scraped the
    fallback owner back out of that row, so every later poll read the wrong board. The sync no
    longer follows a ticket's repository at all - this snapshot is a copy of the row, not
    GitHub's word - and the two stay apart regardless.
    """

    repository_id: int
    project_number: int
    board_owner: str
    snapshot: RepositorySnapshot
    # The server the repository is registered to, which a thread's shut is asked against before it
    # is attempted. Issue #198.
    guild_id: int

    @classmethod
    def of(cls, repository: Repository, project_number: int) -> _Board:
        """The board a repository has linked, addressed the one way `domain.board` writes down."""
        owner, _, name = repository.repo_name.partition("/")
        return cls(
            repository_id=repository.id,
            project_number=project_number,
            board_owner=owner_of_board(
                project_owner=repository.project_owner, repo_name=repository.repo_name
            ),
            snapshot=RepositorySnapshot(
                github_repo_id=repository.github_repo_id,
                owner=owner,
                name=name,
                html_url=repository.repo_url,
            ),
            guild_id=repository.discord_guild_id,
        )
