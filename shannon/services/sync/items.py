from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import ThreadNotFoundError
from shannon.discord_bot.threads import LocksThread, OpensThreads
from shannon.domain.enums import ActorRole, Status
from shannon.domain.errors import PermanentError, WrongPolicyError
from shannon.domain.models import Actor, TrackedSnapshot
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.sync.policies import SyncPolicy
from shannon.services.sync.staleness import is_superseded
from shannon.services.sync.threads import ItemThreads, ThreadTarget, ThreadWrite

logger = logging.getLogger(__name__)

SnapshotParser = Callable[[str, Mapping[str, Any]], TrackedSnapshot | None]


class SyncOutcome(StrEnum):
    SYNCED = "synced"
    NOT_TRACKED = "not_tracked"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a sync did.

    The outcome is explicit rather than signalled by returning nothing, because callers give
    different answers for a repository nobody registered and an event that arrived late.
    """

    outcome: SyncOutcome
    tracked_item_id: int | None = None
    thread_id: int | None = None
    message_id: int | None = None
    created: bool = False
    notified: tuple[str, ...] = ()

    @property
    def synced(self) -> bool:
        return self.outcome is SyncOutcome.SYNCED


class SyncsItems(Protocol):
    """Mirroring one snapshot, which is all any caller of this path asks for."""

    async def sync(self, snapshot: TrackedSnapshot, *, settles_the_lock: bool = True) -> SyncResult:
        """`settles_the_lock` is False for a caller that takes the lock itself afterwards.

        Only `/set_done` and the commands beside it do, and they own it: they decide the status
        the lock follows from, they report a refusal to the person who ran them rather than
        failing everything before it, and they lock the thread the render actually wrote to. A
        second attempt from in here would take the refusal away from them and fail the command
        for something they were built to survive.
        """
        ...


class OpensAndLocksThreads(LocksThread, OpensThreads, Protocol):
    """Both thread roles, which only the wiring below needs.

    The service locks and the binding opens. Nothing holds this except the function that builds
    one from the other.
    """


class Notifier(Protocol):
    """Telling the people on an item that they are on it.

    Named here because this is the only thing the sync path asks of it. Who gets told and in
    what words is the notifier's business, not this module's.
    """

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int
    ) -> tuple[str, ...]: ...


class ThreadBinding(Protocol):
    """Keeping one item pointed at one thread, whatever Discord does in between."""

    async def write(self, target: ThreadTarget, *, name: str, content: str) -> ThreadWrite: ...


class ItemSyncService:
    """The one path a GitHub object takes into Discord.

    Pull requests and issues share this, and so do the webhook pipeline and the manual
    commands. What differs between object types lives in the policy, which is the only way two
    kinds of item can stay consistent without two copies of this.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: LocksThread,
        policy: SyncPolicy,
        binding: ThreadBinding,
        notifier: Notifier | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._binding = binding
        self._policy = policy
        self._notifier = notifier

    async def sync(self, snapshot: TrackedSnapshot, *, settles_the_lock: bool = True) -> SyncResult:
        """Bring Discord in line with a snapshot."""
        if snapshot.object_type is not self._policy.object_type:
            # Wiring, not input: a policy paired with the wrong kind of snapshot would file the
            # item under the wrong type and read fields the snapshot may not have. Nothing can
            # make that succeed, so it fails once and loudly rather than sixteen times. MVP 4
            # adds a third object type, which is when this becomes easy to get wrong.
            raise WrongPolicyError(
                f"{type(self._policy).__name__} was handed a {snapshot.object_type.value} "
                f"snapshot for {snapshot.repository.full_name}#{snapshot.number}"
            )

        decision = await self._record(snapshot)
        # The database step either hands over work to do or answers on its own.
        if isinstance(decision, SyncResult):
            if decision.outcome is SyncOutcome.STALE:
                await self._settle_a_lock_still_owed(decision.tracked_item_id, decision.thread_id)
            return decision
        state = decision

        # Discord is called outside the transaction. Holding one open across a network call
        # would let a slow gateway block the database, and a rollback would throw away work
        # Discord had already done.
        wants_locked = state.wants_locked

        # Unlocking comes first and locking last, so that everything in between happens on an
        # open thread. Not because the bot cannot write to a locked one, which it can and which
        # `test_a_locked_thread_still_accepts_metadata_updates` pins: because a reader arriving
        # mid-sync should never find a thread locked against a state it has not been given yet.
        if wants_locked is False and state.thread_id is not None:
            # A thread that has been deleted is rebuilt by the write below, and a new thread is
            # never locked. Letting this raise instead would stop the rebuild ever running: an
            # open issue always unlocks, so a deleted thread would end its mirror for good.
            with contextlib.suppress(ThreadNotFoundError):
                await self._threads.set_locked(thread_id=state.thread_id, locked=False)
                await self._note_the_lock(state.tracked_item_id, state.thread_id, locked=False)

        written = await self._binding.write(
            state.target, name=state.thread_name, content=state.metadata
        )
        handle = written.handle

        notified: tuple[str, ...] = ()
        if self._notifier is not None:
            notified = await self._notifier.notify(
                tracked_item_id=state.tracked_item_id,
                thread_id=handle.thread_id,
                guild_id=state.guild_id,
            )

        # Two reasons to shut it, kept apart because they are answered by different things.
        #
        # The payload asked for it, and the guard is about a payload that may have been
        # superseded while this sync was in Discord. A lock decided from the row has nothing to
        # be superseded by: the row is what a newer sync would have written, and it was read
        # after that sync committed or not at all.
        asked_for = wants_locked is True and (
            state.locked_from_the_row or await self._still_current(state.tracked_item_id, snapshot)
        )

        # Or a thread has just been opened, and one this bot opens belongs in the state the item
        # is in. For a pull request the payload cannot say what that is: its lock is taken by
        # `/set_done` alone and lives in the row. So a finished pull request whose thread
        # somebody deleted came back with a replacement anybody could post in, above a block
        # reading DONE, and nothing here ever shut one, because `PullRequestPolicy.locked`
        # answers None for every snapshot and both calls that could have are skipped on that.
        # Running `/set_done` again does restore it, and nothing tells anybody to.
        #
        # The policy is asked rather than read off `locked` answering None, which a ticket does
        # too and for the opposite reason. A card in the Done column is DONE on the row because
        # the board says so and nobody shut its thread; shutting a replacement would invent a
        # lock the original never had, and nothing would ever take it off again, which is the
        # thread nobody can answer in that `TicketPolicy.locked` refuses to make.
        #
        # Decided from what the row remembers rather than from whether this attempt was the one
        # that opened the thread. That was true for exactly one delivery attempt, and three
        # ordinary things can fail after the thread is claimed onto the row and before the lock
        # lands: the lock refused, the reviewer ping refused, a thread that opens but cannot be
        # written to. Any of them and the retry found the thread already there, asked for
        # nothing, and recorded the delivery handled, which left the replacement open for good.
        #
        # An ordinary delivery for a finished pull request still costs no Discord call, because
        # the row says the thread is already shut. That is what answering None was protecting.
        #
        # `written.created` as well as the row, because the row was read before the write and a
        # thread opened since is not the thread it was describing. `claim_thread` clears the
        # answer when the pointer moves, so the row catches up a moment later; this is the same
        # fact, a moment earlier.
        shut_by_the_row = (
            settles_the_lock
            and self._policy.lock_lives_in_the_row
            and state.shut_when_opened
            and (written.created or state.thread_locked is not True)
        )

        if asked_for or shut_by_the_row:
            await self._shut(state.tracked_item_id, handle.thread_id, asked_for=asked_for)

        return SyncResult(
            outcome=SyncOutcome.SYNCED,
            tracked_item_id=state.tracked_item_id,
            thread_id=handle.thread_id,
            message_id=handle.message_id,
            created=written.created,
            notified=notified,
        )

    async def _settle_a_lock_still_owed(self, tracked_item_id: int, thread_id: int | None) -> None:
        """Shut a thread the item is owed, even where the delivery itself is out of date.

        A superseded delivery is turned away because a newer one has already done the work, and
        that is right about everything the payload says. It is wrong about the lock exactly once:
        the rebuild path runs only for an item with no thread, and attaching the thread is
        committed before the Discord work that follows, so the attempt that rebuilds a deleted
        thread also arms the guard against its own retry. One 503 on the lock after that and
        every retry is turned away, reported as handled, and the lock is dropped. A closed issue
        sends no further event, so nothing else was ever coming for it.

        Only the lock. Letting the delivery through instead reaches the write, and the write
        rewrites the live thread's block from a payload that is out of date: the reviewers, the
        assignees, the tags and every mention revert, and for a finished item nothing comes along
        afterwards to put them back. Being late about a lock is worth fixing; being late about
        everything else is what the guard is for.

        The row is read again rather than carried down, because the answer wanted here is the
        row's and not the payload's, and this runs after the transaction that read it has closed.
        """
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
        # The thread cannot be missing here, because both answers of STALE require the item to
        # have one, but it is checked with the rest rather than trusted: the cost of being wrong
        # about that is a Discord call made against nothing.
        if item is None or thread_id is None or item.discord_thread_locked is True:
            return
        if not self._policy.shut_by_the_row(status=item.status, github_state=item.github_state):
            return
        await self._shut(tracked_item_id, thread_id, asked_for=False)

    async def _shut(self, tracked_item_id: int, thread_id: int, *, asked_for: bool) -> None:
        """Close the thread, and write down that it is closed.

        Writing it down is what makes a second attempt possible. The lock used to be asked for
        only on the delivery attempt that opened the thread, and that fact lives for one attempt:
        anything failing after the thread was claimed onto the row and before the lock landed
        left the retry finding a thread already there, asking for nothing, and recording the
        delivery handled. A finished pull request kept a thread anybody could post in above a
        block reading DONE, and nothing revisited it.

        A permission this bot has never been granted is a different answer from a bad moment, and
        only where the row is what asked. Failing the delivery over one would drop it on the first
        attempt, and every later event for the item would drop the same way, for a thread nobody
        can shut until somebody grants the permission. It is said and stepped over instead, with
        the row left saying the thread is not shut, so granting it later is enough on its own. A
        bad moment still fails the delivery, because that is what gets it another go.

        A payload that asked for the lock fails on either, which is what it did before any of
        this: that delivery is answering a state change somebody made, and dropping it silently
        would leave a closed issue looking open with nothing recorded anywhere.
        """
        try:
            await self._threads.set_locked(thread_id=thread_id, locked=True)
        except PermanentError as refusal:
            if asked_for:
                raise
            logger.warning(
                "could not shut the thread for tracked item %s: %s", tracked_item_id, refusal
            )
            return
        await self._note_the_lock(tracked_item_id, thread_id, locked=True)

    async def _note_the_lock(self, tracked_item_id: int, thread_id: int, *, locked: bool) -> None:
        """Its own transaction, because the Discord call it records happens outside one."""
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).note_the_lock(
                tracked_item_id, thread_id=thread_id, locked=locked
            )

    async def _still_current(self, tracked_item_id: int, snapshot: TrackedSnapshot) -> bool:
        """Whether a newer sync has been through this item since this one read it.

        Only the database half of a sync is ordered. The Discord half happens outside any
        transaction, so `/pr` running beside the worker can interleave with it, and locking is
        the step where that shows: it is last, and it is decided from a snapshot that may
        already have been superseded. Left alone, a reopened issue can end up in a thread
        nobody can post in, and unlike a stale metadata block that does not right itself.

        Strictly newer, because this sync has already written its own timestamp.
        """
        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
            stored = item.github_updated_at if item is not None else None

        if not is_superseded(snapshot.updated_at, stored):
            return True

        logger.info(
            "not locking tracked item %s: a newer sync has been through since", tracked_item_id
        )
        return False

    async def _record(self, snapshot: TrackedSnapshot) -> _SyncState | SyncResult:
        """The database half, in one transaction: work out where this goes, then write it.

        Split in two because the two halves fail differently. Resolving can decide there is
        nothing to do at all, and writing cannot.
        """
        async with self._sessionmaker() as session, session.begin():
            placement = await self._resolve(session, snapshot)
            if isinstance(placement, SyncResult):
                return placement
            return await self._write(session, snapshot, placement)

    async def _resolve(
        self, session: AsyncSession, snapshot: TrackedSnapshot
    ) -> _Placement | SyncResult:
        """Find the repository, the channel and the item, or give a reason there is no work."""
        object_type = self._policy.object_type

        repository = await RepositoryStore(session).get_by_github_id(
            snapshot.repository.github_repo_id
        )
        if repository is None:
            # Not debug: a repository somebody registered going missing, or a webhook installed
            # across an organisation, is the likeliest reason for "the bot has stopped posting"
            # and the only place it is ever said.
            logger.info(
                "%s is not registered to any guild, ignoring %s.%s",
                snapshot.repository.full_name,
                object_type.value,
                snapshot.action,
            )
            return SyncResult(outcome=SyncOutcome.NOT_TRACKED)

        channels = ChannelMappingStore(session)
        channel = await channels.get(repository.id, object_type)
        if channel is None and self._policy.channel_fallback is not None:
            channel = await channels.get(repository.id, self._policy.channel_fallback)
        if channel is None:
            logger.warning(
                "%s has no channel mapped for %s, run /set_channel",
                snapshot.repository.full_name,
                object_type.value,
            )
            return SyncResult(outcome=SyncOutcome.NOT_TRACKED)

        # Held for the rest of the transaction, because everything below is decided from what
        # this read says and then written. Two syncs of one item overlap by design: `/pr` runs
        # while the worker is mid-delivery, GitHub sends several events for one item at once, and
        # a second replica leases in parallel. Without the lock both read the row before either
        # commits, so both answer "not superseded" and the one carrying the older payload writes
        # its whole snapshot over the newer one, down to deleting a reviewer's row with its
        # `notified_at` and putting back somebody the newer payload had removed, who is then
        # pinged again. An item nobody has created yet has no row to lock, so it cannot be
        # judged here at all. `_write` judges that one, at the point the row exists.
        item = await TrackedItemStore(session).get(
            repository_id=repository.id,
            object_type=object_type,
            github_object_id=snapshot.github_object_id,
            lock=True,
        )
        superseded = item is not None and is_superseded(snapshot.updated_at, item.github_updated_at)

        if superseded and item.discord_thread_id is not None:
            logger.info(
                "ignoring a stale %s.%s for %s#%s",
                object_type.value,
                snapshot.action,
                snapshot.repository.full_name,
                snapshot.number,
            )
            return SyncResult(
                outcome=SyncOutcome.STALE,
                tracked_item_id=item.id,
                thread_id=item.discord_thread_id,
                message_id=item.discord_message_id,
            )

        return _Placement(
            repository=repository,
            channel_id=channel.discord_channel_id,
            item=item,
            superseded=superseded,
        )

    async def _write(
        self, session: AsyncSession, snapshot: TrackedSnapshot, placement: _Placement
    ) -> _SyncState | SyncResult:
        """Bring the stored item in line with the snapshot, and render what Discord will show."""
        object_type = self._policy.object_type
        repositories = RepositoryStore(session)
        items = TrackedItemStore(session)

        item = placement.item
        superseded = placement.superseded
        if item is None:
            item = await items.get_or_create(
                repository_id=placement.repository.id,
                object_type=object_type,
                github_object_id=snapshot.github_object_id,
                github_object_number=snapshot.number,
                github_url=snapshot.html_url,
                title=snapshot.title,
                github_state=snapshot.display_state,
                status=Status.NOT_REVIEWED,
                priority=snapshot.priority,
                github_updated_at=snapshot.updated_at,
            )
            # The same question `_resolve` asks, asked again because it could not be asked there.
            # A brand-new item has no row to lock, so both syncs of one arrive here believing they
            # are current, and the loser then wrote the older payload over the newer: the title
            # and the state, and the reviewers, whose rows `replace` deletes outright along with
            # the `notified_at` saying they had already been told. An item opened with a reviewer
            # on it is two deliveries milliseconds apart, so this is the ordinary case, not a
            # corner of one.
            #
            # Asking here works because the insert above is what serialises them. On a conflict
            # it waits on the unique index until the sync that got there first commits, so by the
            # time it returns, that sync's timestamp is on the row and held. Nothing to compare
            # against means this sync wrote the row itself, and a mark equal to its own snapshot
            # reads as current, which it is.
            superseded = is_superseded(snapshot.updated_at, item.github_updated_at)
            if superseded and item.discord_thread_id is not None:
                logger.info(
                    "ignoring a stale %s.%s for %s#%s, another sync created it first",
                    object_type.value,
                    snapshot.action,
                    snapshot.repository.full_name,
                    snapshot.number,
                )
                return SyncResult(
                    outcome=SyncOutcome.STALE,
                    tracked_item_id=item.id,
                    thread_id=item.discord_thread_id,
                    message_id=item.discord_message_id,
                )

        # Only once the delivery is known to be current. Every payload carries the repository's
        # name as of the moment it was sent, so following one that arrived late puts the old name
        # back.
        #
        # The guard belongs here and not only in `_resolve`. An item whose thread has gone is
        # deliberately not turned away as stale, because the thread has to be rebuilt however old
        # the delivery is, and that one path reached this line with a superseded payload and
        # rolled the repository's name and URL back to whatever GitHub called it before a rename.
        # Nothing rewrites the row afterwards, so it stayed wrong until the next current delivery,
        # which for a quiet repository may never come. The rest of this method already refuses to
        # believe such a payload about anything; the name is the piece that was believing it.
        #
        # Below the item, because until the row is there the answer above is not known yet.
        if not superseded:
            await repositories.follow_rename(
                placement.repository,
                repo_name=snapshot.repository.full_name,
                repo_url=snapshot.repository.html_url,
            )

        roles = self._policy.assignments(snapshot)
        if superseded:
            # The item lost its thread, so one gets built however old this delivery is. That is
            # no reason to believe the payload about anything else: adopting it would put back a
            # title since changed and swap the people for whoever was on the item then, deleting
            # the ones since added and pinging the ones since removed. Stale metadata is
            # corrected by the next delivery; a ping cannot be taken back.
            logger.info(
                "rebuilding a thread for %s#%s from an old %s.%s, keeping what is stored",
                snapshot.repository.full_name,
                snapshot.number,
                object_type.value,
                snapshot.action,
            )
            roles = {}
        else:
            self._apply(items, item, snapshot)
            await self._store_people(session, item.id, roles, snapshot)

        # People only. A team slug and a GitHub login are separate namespaces on GitHub's side,
        # and `/link` lets anybody bind a name to their own account without GitHub being asked
        # whether it is theirs, so a member can claim `security` and the account map cannot tell
        # that from the real thing.
        #
        # Belt and braces, and worth saying which is which. The renderer is what actually keeps a
        # claimed slug out of a thread: it names teams plainly and never looks one up here. This
        # keeps the slug out of the map in the first place, so that a later reader of it cannot
        # reopen the hole by looking up something by name without knowing which namespace it came
        # from. Deleting it changes nothing today, which is exactly why it needs saying.
        people = {
            actor.login: actor.github_user_id
            for role, actors in roles.items()
            if role is not ActorRole.REVIEWER_TEAM
            for actor in actors
        }
        mentions = await UserLinkStore(session).resolve_many(
            guild_id=placement.repository.discord_guild_id, people=people
        )

        # What the thread is told, and what its lock is set from. The same snapshot everywhere
        # else, and on the superseded branch above the row instead, for the fields the row holds.
        #
        # That branch already refuses this payload about the title, the state, the status and
        # the people, because a delivery from before a close is wrong about all of them. It went
        # on to render the block from it anyway, so a merged pull request whose thread somebody
        # deleted was rebuilt saying `State: Open`, and a closed issue was rebuilt with `State:
        # Open` sitting directly above `Status: DONE`. The window that was accepted for a stale
        # block is only a window while another delivery is coming; a merged pull request and a
        # closed issue send no more, so it was permanent.
        #
        # The lock was read off the same payload, which left the rebuilt thread of a closed issue
        # open, and nothing else ever locks one.
        #
        # People are not corrected here. They live in their own table rather than on this row,
        # and the empty mention map the superseded branch leaves behind is what stops the rebuild
        # pinging whoever was on the item at the time.
        shown = (
            replace(
                snapshot,
                title=item.title,
                state=item.github_state,
                html_url=item.github_url,
            )
            if superseded
            else snapshot
        )
        return _SyncState(
            tracked_item_id=item.id,
            guild_id=placement.repository.discord_guild_id,
            channel_id=placement.channel_id,
            thread_id=item.discord_thread_id,
            message_id=item.discord_message_id,
            metadata=self._policy.render(
                shown, status=item.status, priority=item.priority, mentions=mentions
            ),
            thread_name=self._policy.thread_name(shown),
            wants_locked=self._policy.locked(shown),
            locked_from_the_row=superseded,
            shut_when_opened=item.status is Status.DONE,
            thread_locked=item.discord_thread_locked,
        )

    def _apply(self, items: TrackedItemStore, item: TrackedItem, snapshot: TrackedSnapshot) -> None:
        item.title = snapshot.title
        item.github_url = snapshot.html_url
        item.github_object_number = snapshot.number
        item.github_state = snapshot.display_state
        item.status = self._policy.status_for(snapshot, item.status)
        item.priority = snapshot.priority
        if snapshot.updated_at is not None:
            # An item with no thread is deliberately never treated as stale, so a snapshot older
            # than what is stored can reach here. The store is what keeps the mark from moving
            # backwards when it does.
            items.raise_updated_at(item, snapshot.updated_at)

    async def _store_people(
        self,
        session: AsyncSession,
        tracked_item_id: int,
        roles: Mapping[ActorRole, Sequence[Actor]],
        snapshot: TrackedSnapshot,
    ) -> None:
        """Make the stored people match the payload, and reopen anything it asks for again.

        `as_of` is when GitHub says this payload was current. A request already closed by a
        review is only reopened by a payload newer than that review, which is what separates
        somebody clicking re-request from a delivery that has been retrying since before it.

        Two ways of being asked again, because there are two ways a request ends. One is closed
        here, by a review we were told about, and its stamp is what a later payload is measured
        against. The other is closed by GitHub alone and never announced, and the only evidence
        of it is this event naming the party at the top level. That evidence is only worth acting
        on once, and the row is what says whether it already has been: it carries the age of the
        request it represents, so a delivery replayed measures equal against it and a genuine
        second ask does not.
        """
        as_of = snapshot.updated_at
        assignments = ItemAssignmentStore(session)
        asked = self._policy.asked_again(snapshot)
        for role, actors in roles.items():
            await assignments.replace(
                tracked_item_id=tracked_item_id, role=role, actors=actors, as_of=as_of
            )
            reopened = [
                *await assignments.reopen_if_newer(
                    tracked_item_id, role, [actor.login for actor in actors], as_of
                ),
                *await assignments.reopen_request(
                    tracked_item_id, role, [actor.login for actor in asked.get(role, ())], as_of
                ),
            ]
            if reopened:
                logger.info("review requested again from %s", ", ".join(reopened))


def build_item_sync(
    sessionmaker: async_sessionmaker,
    threads: OpensAndLocksThreads,
    policy: SyncPolicy,
    notifier: Notifier | None = None,
) -> ItemSyncService:
    """Assemble a sync service and the thread binding it drives.

    The service locks threads and nothing else, so its constructor asks for nothing else. The
    binding is what opens and rewrites them, and it needs a wider handle; composing the two is
    this function's whole job.
    """
    return ItemSyncService(
        sessionmaker, threads, policy, ItemThreads(sessionmaker, threads), notifier
    )


def build_item_handler(service: SyncsItems, parse: SnapshotParser) -> EventHandler:
    """Adapt a webhook event to the sync service.

    Only the parser differs between object types, so both kinds of event share this rather
    than each having its own near-identical handler module.
    """

    async def handle(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        snapshot = parse(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED
        result = await service.sync(snapshot)
        return WebhookOutcome.PROCESSED if result.synced else WebhookOutcome.IGNORED

    return handle


@dataclass(frozen=True, slots=True)
class _Placement:
    """Where a snapshot belongs, once the database has been asked.

    `superseded` means an older delivery reached an item that has lost its thread. The thread
    still gets rebuilt; nothing else in the payload is believed.
    """

    repository: Repository
    channel_id: int
    item: TrackedItem | None
    superseded: bool


@dataclass(frozen=True, slots=True)
class _SyncState:
    """Work for the Discord step, only ever built when there is work to do."""

    tracked_item_id: int
    guild_id: int
    channel_id: int
    metadata: str
    # The thread's name, rendered beside the block it belongs with rather than in the caller,
    # so the two cannot end up describing different states.
    thread_name: str
    thread_id: int | None
    message_id: int | None
    # Where the thread's lock belongs, or None for a kind whose lock this path does not touch.
    # Decided in `_write`, where the payload and the row are both in hand, because on the
    # rebuild path they disagree and only one of them is to be believed.
    wants_locked: bool | None
    # Whether that answer came from the row rather than from the payload, which decides whether
    # the staleness guard below applies to it at all.
    locked_from_the_row: bool
    # Whether a thread opened by this sync belongs shut, read off the row's status. Only for a
    # kind whose lock the payload says nothing about.
    shut_when_opened: bool
    # What the row remembers this bot last making the lock on the thread it points at. Null means
    # it has not set one, which is what a thread just opened is and what every row written before
    # the column existed says.
    thread_locked: bool | None

    @property
    def target(self) -> ThreadTarget:
        return ThreadTarget(
            tracked_item_id=self.tracked_item_id,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            message_id=self.message_id,
        )
