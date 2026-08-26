"""Moving an item through the workflow: its status and its priority.

GitHub is written first and Discord second, which the requirements ask for and which is also the
only order that can be recovered from. The labels are the record; the stored status and the
metadata block are a mirror of them, so a run that dies half way leaves the item correct on
GitHub and stale here, and the next event or the next command corrects it. The other order
leaves Discord claiming something GitHub never agreed to.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.threads import LocksThread
from shannon.domain.enums import ObjectType, Priority, Status
from shannon.domain.errors import ItemNotReadyError, PermanentError, ShannonError
from shannon.domain.models import Label, TrackedSnapshot
from shannon.github import labels
from shannon.github.client import GitHubClient
from shannon.services.sync.items import SyncsItems

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
        threads: LocksThread,
        kinds: Mapping[ObjectType, ItemKind],
    ) -> None:
        self._sessionmaker = sessionmaker
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
            lock = await self._set_lock(thread_id, wants_lock)
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
        previous = await self._store_status(found.tracked_item_id, status)
        written = await self._rerender(found, snapshot, change)

        # Touched only when DONE is on one side of the move or the other, so an ordinary status
        # change still costs no Discord call. Moving OUT of DONE has to give the thread back:
        # `PullRequestPolicy.locked` returns None on every sync, so the lock `/set_done` takes is
        # the only one a pull request ever gets and nothing else was ever going to lift it. The
        # commands to move it back are all allowed and all reported success, and left the thread
        # shut against the discussion they had just reopened.
        wants_lock = status is Status.DONE
        lock = (
            await self._set_lock(written or thread_id, wants_lock)
            if wants_lock or previous is Status.DONE
            else _Lock(locked=False)
        )

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
        self, found: _Found, snapshot: TrackedSnapshot, change: labels.LabelChange
    ) -> int | None:
        """Bring the thread in line, through the same path a webhook takes.

        The snapshot is carried forward with its labels corrected rather than fetched again.
        Re-syncing the one that was read before the write would take the priority straight back
        off the labels it no longer has, which is the change undoing itself.

        Answers with the thread that was actually written to. It is usually the one the command
        was run in, and is not when somebody deleted that thread in between: the sync opens a
        replacement, and locking the id the command arrived on would lock nothing.
        """
        result = await self._kinds[found.object_type].sync.sync(_relabelled(snapshot, change))
        return result.thread_id

    async def _set_lock(self, thread_id: int, locked: bool) -> _Lock:
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
        """
        try:
            await self._threads.set_locked(thread_id=thread_id, locked=locked)
        except DiscordGatewayError as error:
            logger.warning("could not set the lock on thread %s: %s", thread_id, error.message)
            return _Lock(locked=False, refused=True, permanent=isinstance(error, PermanentError))
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
            status=item.status,
            priority=item.priority,
        )


def build_item_workflow(
    sessionmaker: async_sessionmaker,
    github: GitHubClient,
    threads: LocksThread,
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
