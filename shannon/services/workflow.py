"""Moving an item through the workflow: its status and its priority.

GitHub is written first and Discord second, which the requirements ask for and which is also the
only order that can be recovered from. The labels are the record; the stored status and the
metadata block are a mirror of them, so a run that dies half way leaves the item correct on
GitHub and stale here, and the next event or the next command corrects it. The other order
leaves Discord claiming something GitHub never agreed to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.domain.errors import ItemNotReadyError, PermanentError, ShannonError
from shannon.domain.models import Label, TrackedSnapshot
from shannon.github import labels
from shannon.github.client import GitHubClient
from shannon.services.sync.items import LocksAndKnowsServers, SyncsItems
from shannon.services.sync.one_at_a_time import ItemLock

logger = logging.getLogger(__name__)

# Owner, name, number.
Fetcher = Callable[[str, str, int], Awaitable[TrackedSnapshot]]

# A pull request is only finished once somebody has said it is ready to merge. The requirement
# is about the order of a review, not about bookkeeping: marking a pull request done skips the
# step where a reviewer says it may be merged, and locking its thread takes away the place that
# would have been said.
DONE_NEEDS = Status.READY_FOR_MERGE


class NotAnItemThreadError(ShannonError):
    """The command was run somewhere that is not a tracked item's thread."""


class WorkflowRefusedError(ShannonError):
    """The change is not one this item can be given right now."""


@dataclass(frozen=True, slots=True)
class ItemKind:
    """How to read and re-render one kind of item.

    Both halves differ by object type and neither belongs here: fetching is the client's, and
    rendering is the sync service's. The command cannot pick between them because it only knows
    which thread it is in, so the picking happens here.
    """

    fetch: Fetcher
    sync: SyncsItems


@dataclass(frozen=True, slots=True)
class _Lock:
    """What became of the one Discord call a status change makes, and whether to ask again."""

    locked: bool
    refused: bool = False
    permanent: bool = False
    # The thread this was asked to lock is not there any more, which is a different answer from
    # a refusal: nothing about the lock is wrong and asking again for the same thread can only
    # fail the same way. What it needs is the thread rebuilt.
    thread_missing: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """What the person who ran the command is told."""

    full_name: str
    number: int
    changed: bool
    locked: bool = False
    # Set when Discord refused the lock step, with the direction that was asked for. What to
    # tell somebody about a thread that would not lock and one that would not unlock is not the
    # same sentence: the second one means nobody can reply in it.
    lock_refused: bool = False
    wanted_locked: bool = False
    # Whether asking again could ever work. A missing permission cannot be waited out, and the
    # board poller is the one caller with nobody to tell, so it is the one that has to know the
    # difference between a refusal worth another poll and one that will refuse every poll.
    lock_refusal_is_permanent: bool = False


class LabelsItems(Protocol):
    """Putting a label on an item and taking one off, which is all this path asks of GitHub."""

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None: ...


class ItemWorkflow:
    """Backs the status and priority commands.

    Every one of them is the same three steps with a different label: read the item as GitHub
    has it, put the labels right there, then bring the stored copy and the thread into line.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        github: LabelsItems,
        threads: LocksAndKnowsServers,
        kinds: Mapping[ObjectType, ItemKind],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._one_item = ItemLock(sessionmaker)
        self._github = github
        self._threads = threads
        self._kinds = kinds

    async def set_status(self, *, thread_id: int, status: Status) -> WorkflowOutcome:
        """Move an item to a status, and lock its thread once it is done."""
        found = await self._locate(thread_id)
        self._refuse_a_kind_it_cannot_move(found)
        snapshot = await self._fetch(found)
        self._refuse_a_status_that_will_not_hold(found, snapshot, status)

        change = labels.status_change(snapshot.label_names, status)
        if change.nothing_to_do and found.status is status:
            # Nothing to write, which is not the same as nothing to do. The lock is the last step
            # of a status change and the likeliest to have been refused on its own, so a repeat
            # is what gets it a second go.
            #
            # Both directions, and it used to be only one. Leaving DONE has to give the thread
            # back, and a refused unlock had nothing anywhere to try it again: the row already
            # says the new status, so the branch below never touches the lock either, and
            # `PullRequestPolicy.locked` returns None so no sync, webhook or `/pr` ever unlocks a
            # pull request's thread. One 503 shut a reopened pull request against the discussion
            # it had just been reopened for, permanently, while every later command answered that
            # it was already where it was being put.
            #
            # A closed issue cannot reach here asking to be unlocked: the guard above refuses any
            # status but DONE for one. So the only thread this ever opens is one this path shut.
            wants_lock = status is Status.DONE
            # Held across the lock this sets, because this is a Discord call the sync path never
            # makes and so never covered. An event for the same item can be in its own Discord
            # phase right now, and locking is the step where interleaving shows: it is last on
            # both sides and decided from what each read before it started. The rebuild inside
            # goes through the ordinary sync, which takes this same lock and is let straight
            # through, because a caller already holding it is not something to wait for.
            async with self._one_item.held(found.github_object_id):
                if not await self._row_still_says(found.tracked_item_id, status):
                    # Somebody moved the item while this waited, and their lock is the current
                    # one. Everything this branch acts on was read before the wait: the status
                    # off `_locate`, the labels off a GitHub round trip after it. The branch
                    # below never had this problem because it re-reads under the row's own lock
                    # and decides from what that gave back.
                    #
                    # Setting the lock anyway is not a stale write that rights itself. It shuts
                    # a thread against a state the row no longer holds, and for a pull request
                    # nothing lifts one: `PullRequestPolicy.locked` answers None on every sync
                    # and `shut_by_the_row` reads DONE off a row that has moved on, so no
                    # webhook, sync or `/pr` reopens it and the reviewers are shut out of the
                    # discussion they had just reopened until somebody thinks to run a status
                    # command again.
                    logger.info(
                        "not setting the lock on %s#%s: it moved to %s while this waited",
                        found.full_name,
                        found.number,
                        status.value,
                    )
                    return WorkflowOutcome(found.full_name, found.number, changed=False)

                lock = await self._set_lock(thread_id, wants_lock, guild_id=found.guild_id)
                if lock.thread_missing:
                    thread_id, lock = await self._rebuild_and_lock(
                        found, snapshot, change, wants_lock, thread_id
                    )
                await self._write_the_lock_down(found.tracked_item_id, thread_id, lock)
            return WorkflowOutcome(
                found.full_name,
                found.number,
                changed=False,
                locked=lock.locked,
                lock_refused=lock.refused,
                wanted_locked=wants_lock,
                lock_refusal_is_permanent=lock.permanent,
            )

        await self._apply(found, change)
        # Everything from the row write to the lock, held to this one writer. The labels
        # above are GitHub's and are left outside it: what two writers of one item can
        # spoil for each other is the thread, and holding a connection across a rate
        # limited GitHub call would be paying for the wrong thing.
        async with self._one_item.held(found.github_object_id):
            previous = await self._store_status(found.tracked_item_id, status)
            try:
                written = await self._rerender(found, snapshot, change, settles_the_lock=False)
            except BaseException:
                # BaseException because being cancelled counts as a failure here. The poller is
                # cancelled where it stands when the process is asked to stop, and a card being
                # moved at that moment would otherwise keep a row nothing in Discord shows, which
                # the next poll after a restart writes off. Shielded so the cancellation cannot
                # interrupt the putting back as well; under it the await returns at once and the
                # write lands a moment later.
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self._give_the_status_back(found.tracked_item_id, status, previous)
                    )
                raise

            # Touched only when DONE is on one side of the move or the other, so an ordinary
            # status change still costs no Discord call. Moving OUT of DONE has to give the
            # thread back: `PullRequestPolicy.locked` returns None on every sync, so the lock
            # `/set_done` takes is the only one a pull request ever gets and nothing else was
            # ever going to lift it. The commands to move it back are all allowed and all
            # reported success, and left the thread shut against the discussion they had just
            # reopened.
            wants_lock = status is Status.DONE
            touched = wants_lock or previous is Status.DONE
            lock = (
                await self._set_lock(written or thread_id, wants_lock, guild_id=found.guild_id)
                if touched
                else _Lock(locked=False)
            )
            if touched:
                await self._write_the_lock_down(found.tracked_item_id, written or thread_id, lock)

        logger.info("%s#%s set to %s", found.full_name, found.number, status.value)
        return WorkflowOutcome(
            found.full_name,
            found.number,
            changed=True,
            locked=lock.locked,
            lock_refused=lock.refused,
            wanted_locked=wants_lock,
            lock_refusal_is_permanent=lock.permanent,
        )

    async def set_priority(self, *, thread_id: int, priority: Priority) -> WorkflowOutcome:
        """Move an item to a priority. Nothing is locked and no status moves with it.

        The stored priority has to agree as well as the label, which is the same rule the status
        half above follows and for the same reason: a run that puts the label on GitHub and then
        cannot reach Discord leaves the thread saying the old one. Asking GitHub alone, the
        repeat that is meant to repair that answers "already HIGH priority" and writes nothing,
        so the block stays wrong until some unrelated event for the item arrives, which for a
        merged pull request is never. Reproduced: label HIGH on GitHub, HIGH in the row, UNSET
        in the thread, and the command that exists to fix it reporting nothing to do.
        """
        found = await self._locate(thread_id)
        self._refuse_a_kind_it_cannot_move(found)
        snapshot = await self._fetch(found)

        change = labels.priority_change(snapshot.label_names, priority)
        if change.nothing_to_do and found.priority is priority:
            return WorkflowOutcome(found.full_name, found.number, changed=False)

        await self._apply(found, change)
        await self._rerender(found, snapshot, change)

        logger.info("%s#%s set to %s priority", found.full_name, found.number, priority.value)
        return WorkflowOutcome(found.full_name, found.number, changed=True)

    def _refuse_a_kind_it_cannot_move(self, found: _Found) -> None:
        """Refuse a thread whose item this service has no way to write to.

        A project ticket is a draft card on a board. It has no repository page and no labels, so
        there is nothing here to set: its status is the column it sits in, and the board is where
        that gets changed. Without this the dict lookup below raises KeyError, which reaches the
        person who ran the command as "Something went wrong here" and the log as a traceback.
        """
        if found.object_type not in self._kinds:
            raise WorkflowRefusedError(
                f"That thread is a project {found.object_type.value.lower()}, which has no "
                "GitHub labels to set. Move its card on the board instead."
            )

    def _refuse_a_status_that_will_not_hold(
        self, found: _Found, snapshot: TrackedSnapshot, status: Status
    ) -> None:
        """Refuse, rather than write a status that something else is going to overwrite.

        An issue's status is not this service's alone to decide. The requirements make closing
        an issue mean done, and the sync path enforces that on every delivery, so both
        directions of disagreement have to be refused here: marking an open issue done, and
        marking a closed one anything else. Writing either would put the label on GitHub,
        report the change as made, and then have the very next render take it back.
        """
        if found.object_type is ObjectType.ISSUE:
            if snapshot.closed and status is not Status.DONE:
                raise WorkflowRefusedError(
                    f"That issue is closed on GitHub, which is what makes it "
                    f"{Status.DONE.value}. Reopen it there to give it another status."
                )
            if not snapshot.closed and status is Status.DONE:
                raise WorkflowRefusedError(
                    "Close the issue on GitHub to mark it done; that locks the thread too."
                )
            return

        # A pull request is only finished once a reviewer has said it may be merged. Already
        # being DONE passes too: that is a repeat, and a repeat is how a lock that failed on
        # its own gets tried again.
        if status is Status.DONE and found.status not in (DONE_NEEDS, Status.DONE):
            raise WorkflowRefusedError(
                f"A pull request has to be {DONE_NEEDS.value} before it can be marked "
                f"{Status.DONE.value}. This one is {found.status.value}."
            )

    async def _fetch(self, found: _Found) -> TrackedSnapshot:
        """Read the item from GitHub, and refuse anything that is not the repository we mean.

        Everything below this addresses GitHub by the stored `owner/name`, and a name is not an
        identity. GitHub frees one the moment a repository is renamed, transferred or deleted,
        and the stored one goes stale by design: nothing corrects it until an item webhook
        arrives, and for a repository that has been renamed away no webhook ever will.

        So the path this asks about can be somebody else's repository by the time it is asked.
        Unchecked, the labels were written onto their item, the re-render resolved the fetched
        snapshot by its own id and opened a thread in whichever server had registered it, and
        `/set_done` locked that thread rather than the one the command was run in. The reviewer
        was told it worked and their own thread never changed.

        The check is free. The snapshot already carries the id, and comparing it costs no call.
        """
        snapshot = await self._kinds[found.object_type].fetch(found.owner, found.name, found.number)
        if snapshot.repository.github_repo_id != found.github_repo_id:
            raise WorkflowRefusedError(
                f"{found.full_name} is not the repository this server registered any more. "
                "It has been renamed or replaced on GitHub, and somebody else holds that name "
                "now. Register the repository again under its current name."
            )
        return snapshot

    async def _apply(self, found: _Found, change: labels.LabelChange) -> None:
        """Put the labels right on GitHub.

        Removals first. The two states this can be interrupted in are an item with no status
        label and an item with two, and the first is the one a reader can make sense of.
        """
        for name in change.remove:
            await self._github.remove_label(found.owner, found.name, found.number, name)
        if change.add:
            await self._github.add_label(found.owner, found.name, found.number, change.add)

    async def _rerender(
        self,
        found: _Found,
        snapshot: TrackedSnapshot,
        change: labels.LabelChange,
        *,
        settles_the_lock: bool = True,
    ) -> int | None:
        """Bring the thread in line, through the same path a webhook takes.

        The snapshot is carried forward with its labels corrected rather than fetched again.
        Re-syncing the one that was read before the write would take the priority straight back
        off the labels it no longer has, which is the change undoing itself.

        Answers with the thread that was actually written to. It is usually the one the command
        was run in, and is not when somebody deleted that thread in between: the sync opens a
        replacement, and locking the id the command arrived on would lock nothing.
        """
        result = await self._kinds[found.object_type].sync.sync(
            _relabelled(snapshot, change), settles_the_lock=settles_the_lock
        )
        return result.thread_id

    async def _row_still_says(self, tracked_item_id: int, status: Status) -> bool:
        """Whether the row still says what this command is repeating.

        Asked inside the hold and nowhere else. A repeat exists to give a lock that was refused
        another go, and what makes it a repeat is the row already saying what is being asked for.
        That was read before the wait for the hold, and the whole point of the wait is that
        somebody else is writing.
        """
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
        return item is not None and item.status is status

    async def _set_lock(self, thread_id: int, locked: bool, *, guild_id: int) -> _Lock:
        """Close a finished item's thread to further replies, or open it again.

        Last, after the metadata is written. A locked thread still takes this bot's edits, so
        the order is not what makes it work; it is that the lock is the step most likely to be
        refused, and everything before it is worth keeping when it is.

        Answers whether the lock is where it was asked to be, and separately whether Discord
        refused to put it there. Raising instead is what this used to do, and it told the person
        who ran the command that the whole thing had failed, when everything before this had
        landed: the labels are on GitHub, the status is in the row, the thread says so. The two
        readings are a long way apart for somebody deciding whether to run it again, and a
        refusal here is usually one permission rather than anything to wait out.

        Only the gateway errors, and only around this call. Anything else still raises, and
        anything that fails before this still fails the command outright, because then nothing
        did happen.

        Whether a refusal is permanent is not the exception type alone. A bot that has been
        removed from the server is answered exactly as one that was never given the permission,
        and the two want opposite things: nobody grants a permission by waiting, and nobody
        fixes an absence any other way. It matters most where nobody is watching. The board
        poller writes a move off as carried through on a permanent refusal, deliberately, so
        that one missing permission does not put every card ever dragged to Done into a set
        retried once a minute for ever. Filed that way while the bot is out for five minutes,
        the card is recorded as moved with its thread left open and no poll looks at it again.
        """
        try:
            await self._threads.set_locked(thread_id=thread_id, locked=locked)
        except ThreadNotFoundError as error:
            logger.info("thread %s is gone, so there was nothing to lock: %s", thread_id, error)
            return _Lock(locked=False, refused=True, thread_missing=True)
        except DiscordGatewayError as error:
            logger.warning("could not set the lock on thread %s: %s", thread_id, error.message)
            return _Lock(
                locked=False,
                refused=True,
                permanent=isinstance(error, PermanentError) and self._threads.is_in(guild_id),
            )
        return _Lock(locked=locked)

    async def _store_status(self, tracked_item_id: int, status: Status) -> Status:
        """Written before the re-render, because the render reads it back off the row.

        Answers with the status it replaced, read under the row's own lock. Whether the thread
        gets locked or given back is decided from that and not from the read at the top of the
        command, because three GitHub round trips sit in between and two commands overlapping
        across them both decided from a row neither of them still had.

        What that cost: a pull request at READY_FOR_MERGE, a `/set_done` and a `/set_in_review`
        from two reviewers, or from a reviewer and the board poller. The one that was not
        finishing the item read a status that was not DONE yet, so it never asked for the thread
        back, while `/set_done` locked it last. The item was left reading IN_REVIEW with its
        thread shut, both users were told their command had worked, and nothing lifted it:
        `PullRequestPolicy.locked` returns None, so no webhook or sync ever unlocks a pull
        request, and `/set_done` is refused for being exactly what the race made it.
        """
        async with self._sessionmaker() as session, session.begin():
            item = await TrackedItemStore(session).get_by_id(tracked_item_id, lock=True)
            if item is None:
                raise ItemNotReadyError("That item is no longer tracked here.")
            previous = item.status
            item.status = status
            return previous

    async def _write_the_lock_down(self, tracked_item_id: int, thread_id: int, lock: _Lock) -> None:
        """Tell the row what this command just made the thread, so the sync path stops asking.

        These commands own the lock on a pull request: nothing else takes one, and the sync is
        told to leave it alone on this path so a refusal reaches the person who ran the command
        rather than failing everything before it. The row is how the two halves agree. Without
        this it never hears, so it goes on reading a finished pull request as one whose thread
        has not been shut: every later delivery asks Discord to shut a thread already shut, and
        the staleness guard, which lets a delivery through while a lock is owed, lets every
        superseded delivery for that item straight past a guard that exists to stop an old
        payload overwriting newer state.

        Nothing is written for a refusal. The thread is not where it was asked to be, and the row
        saying otherwise is the one mistake that cannot be recovered from here.
        """
        if lock.refused:
            return
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).note_the_lock(
                tracked_item_id, thread_id=thread_id, locked=lock.locked
            )

    async def _rebuild_and_lock(
        self,
        found: _Found,
        snapshot: TrackedSnapshot,
        change: labels.StatusChange,
        wants_lock: bool,
        thread_id: int,
    ) -> tuple[int, _Lock]:
        """Open a replacement for a thread that has gone, and lock that one instead.

        Only reached from the branch with nothing to write, which is the one branch that skips
        the render. Everywhere else the render runs first and rebuilds a missing thread on its
        own, because the write path turns Discord saying a thread is gone into a replacement.

        Here the lock is the only Discord call made, and it is the one step that needs the thread
        to already exist, so a thread deleted while this bot was not connected to hear about it
        could be noticed here and repaired nowhere. For a card on a board that never ended: the
        poller reads a refused lock as a bad moment worth another go, the column is what ends a
        retry and is deliberately not written, and the one thing that would have rebuilt the
        thread sits on the other side of the branch. A GitHub read, a thread fetch and two
        warnings for that card, once a minute, with nothing able to clear it.
        """
        rebuilt = await self._rerender(found, snapshot, change, settles_the_lock=False)
        # The one the command arrived on, where the render answered with nothing. It used to
        # reach for a field `_Found` does not have, which nothing noticed because nothing here
        # is type checked and every route to it is closed by an invariant somewhere else: a
        # repository row is never deleted, a channel mapping is never deleted, and a ticket is
        # refused before this. All true, and none of them stated anywhere near this line.
        on = rebuilt or thread_id
        return on, await self._set_lock(on, wants_lock, guild_id=found.guild_id)

    async def _give_the_status_back(
        self, tracked_item_id: int, written: Status, previous: Status
    ) -> None:
        """Put the row back when the step after it failed, so the move can be asked for again.

        The status has to be written before the thread is rewritten, because the render reads it
        off the row. So a render that fails leaves the row saying a move happened that nobody can
        see, and for a card on a board that is the end of it. The poller's entire retry is the
        column not being recorded, and both of its first-look guards read a card whose status
        already agrees and whose column was never written down as one it has never seen. The move
        it could not finish is written off on the very next poll, and no poll looks at that card
        again, because nothing else rederives a status from a board. A person running the command
        at least sees it fail and can run it again; nobody is standing over the poller.

        Only where the row still says what this wrote, which is enough for one process and not
        for two. Nothing in the deployment stops a second replica, and every replica with a
        project number set polls the same board on the same interval, so the two ask for the same
        status for the same card. The other one, polling while this one is in Discord, finds the
        row already saying DONE and records the column, which is this one's whole retry marker;
        then this one cannot tell that from its own write and puts the row back, undoing a move
        the other had finished. Comparing a stamp instead does not help: the render's own sync
        writes the row before the Discord call it dies on, so the stamp has moved in the ordinary
        single-process case too, and nothing on the row separates somebody else having acted from
        the step being compensated for.

        That is the shape of a compensating write rather than a fault in this guard, and the
        per-item lock does not close it, though it was written down as the thing that would. The
        lock is taken here, around the write and the render and the compensation together, so no
        other writer can be in the middle of this one. What the other poller acts on is not read
        here: it decides from a batch of rows read before its move loop starts, a service and a
        step earlier, so it has already read DONE off the row by the time it asks for anything
        this could hold it out of. Closing it means the decision being made against the row it is
        acted on, not against a snapshot. Until then the poller belongs in one replica, which is
        said where the setting that enables it is defined.

        The label on GitHub is left where it was put. Setting it is idempotent, the next attempt
        sets it again, and somebody reading GitHub in between sees where the card was dragged
        rather than a value that flickers back on its own.

        Its own failure is said out loud rather than raised, because the failure worth reporting
        is the one that brought us here.
        """
        try:
            async with self._sessionmaker() as session, session.begin():
                item = await TrackedItemStore(session).get_by_id(tracked_item_id, lock=True)
                if item is not None and item.status is written:
                    item.status = previous
        except Exception:
            logger.warning(
                "could not put tracked item %s back to %s after the move failed; it now reads "
                "%s with nothing in Discord to show it",
                tracked_item_id,
                previous.value,
                written.value,
                exc_info=True,
            )

    async def _locate(self, thread_id: int) -> _Found:
        """Which item this thread is, as plain values out of the session.

        The repository is fetched rather than read off `item.repository`: that is a lazy
        relationship, and an async session cannot load one on attribute access.
        """
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_thread(thread_id)
            repository = (
                await RepositoryStore(session).get_by_id(item.repository_id)
                if item is not None
                else None
            )
            if item is None or repository is None:
                raise NotAnItemThreadError(
                    "Run this inside the thread of a pull request or issue this bot is tracking."
                )
            return _Found.of(item, repository)


def _relabelled(snapshot: TrackedSnapshot, change: labels.LabelChange) -> TrackedSnapshot:
    """The snapshot as it will be once the change lands, without asking GitHub again.

    Only appends a label the item is not already carrying. An item can hold two labels the same
    reader answers for, such as `HIGH` beside `urgent` or two statuses at once, and the change then
    strips one and adds the canonical name, which the item already had. Appending it regardless
    rendered the tag twice in the thread, and which of the two happened depended on the order
    GitHub returned the labels in, so the same item state produced different blocks.
    """
    gone = {name.casefold() for name in change.remove}
    kept = [label for label in snapshot.labels if label.name.casefold() not in gone]
    if change.add and change.add.casefold() not in {label.name.casefold() for label in kept}:
        kept.append(Label(name=change.add))
    return replace(snapshot, labels=tuple(kept))


@dataclass(frozen=True, slots=True)
class _Found:
    """The item a thread belongs to, as plain values out of its session."""

    tracked_item_id: int
    object_type: ObjectType
    full_name: str
    # What the repository actually is. The name is only what GitHub called it when something
    # last told us, and GitHub frees a name the moment a repository is renamed or deleted.
    github_repo_id: int
    number: int
    # What the item lock is keyed on, and the only id that is the item's own:
    # the number is unique per repository and this is unique across GitHub.
    github_object_id: int
    # The server the thread is in, for telling a refusal from a bot that has been removed
    # apart from a permission it was never given. Discord answers both the same way.
    guild_id: int
    status: Status
    priority: Priority

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]

    @classmethod
    def of(cls, item: TrackedItem, repository: Repository) -> _Found:
        return cls(
            tracked_item_id=item.id,
            object_type=item.github_object_type,
            full_name=repository.repo_name,
            github_repo_id=repository.github_repo_id,
            number=item.github_object_number,
            github_object_id=item.github_object_id,
            guild_id=repository.discord_guild_id,
            status=item.status,
            priority=item.priority,
        )


def build_item_workflow(
    sessionmaker: async_sessionmaker,
    github: GitHubClient,
    threads: LocksAndKnowsServers,
    *,
    pr_sync: SyncsItems,
    issue_sync: SyncsItems,
) -> ItemWorkflow:
    """Assemble the workflow service with a fetcher and a renderer per object type."""
    return ItemWorkflow(
        sessionmaker,
        github,
        threads,
        {
            ObjectType.PR: ItemKind(
                fetch=lambda owner, name, number: github.get_pull_request(owner, name, number),
                sync=pr_sync,
            ),
            ObjectType.ISSUE: ItemKind(
                fetch=lambda owner, name, number: github.get_issue(owner, name, number),
                sync=issue_sync,
            ),
        },
    )
