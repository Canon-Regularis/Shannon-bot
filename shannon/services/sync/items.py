from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import DiscordGatewayError, ThreadNotFoundError
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import KnowsItsServers, Notify, OpensThreads, ShutsThread
from shannon.domain.enums import ActorRole, Status
from shannon.domain.errors import PermanentError, WrongPolicyError
from shannon.domain.json import JsonObject
from shannon.domain.models import Actor, TrackedSnapshot
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.sync.announcements import AnnouncesInThread, Arrival
from shannon.services.sync.one_at_a_time import ItemLock
from shannon.services.sync.policies import SyncPolicy
from shannon.services.sync.staleness import is_superseded
from shannon.services.sync.threads import ItemThreads, ThreadTarget, ThreadWrite

logger = logging.getLogger(__name__)

SnapshotParser = Callable[[str, JsonObject], TrackedSnapshot | None]


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
    # A permission refused the thread being shut. Carried rather than raised, because the closing
    # header is written after the sync and is the only place anybody will read it.
    shut_refused: bool = False
    # The thread this sync moved the item off, for a caller with something to say in it. Only ever
    # set by a binding built to relocate, which is the one behind `/set_channel`.
    displaced: int | None = None

    @property
    def synced(self) -> bool:
        return self.outcome is SyncOutcome.SYNCED


class SyncsItems(Protocol):
    """Mirroring one snapshot, which is all any caller of this path asks for."""

    async def sync(
        self,
        snapshot: TrackedSnapshot,
        *,
        settles_the_lock: bool = True,
        arrived: int | None = None,
    ) -> SyncResult:
        """`arrived` is the number the queue gave this delivery, which is the order it reached
        this bot. It separates two deliveries carrying the same `updated_at`, which GitHub stamps
        to the second so they routinely do. None for a sync that came from a command or the board
        rather than from a delivery, and for one whose caller has no number to offer.

        `settles_the_lock` is False for a caller that takes the lock itself afterwards.

        Only `/set_done` and the commands beside it do, and they own it: they decide the status
        the lock follows from, they report a refusal to the person who ran them rather than
        failing everything before it, and they lock the thread the render actually wrote to. A
        second attempt from in here would take the refusal away from them and fail the command
        for something they were built to survive.
        """
        ...


class ShutsAndKnowsServers(ShutsThread, KnowsItsServers, Protocol):
    """What the sync service itself needs of Discord: the lock, and whether the server is there.

    The second is only ever asked about a refusal, to tell a permission this bot was never given
    from a server it is no longer in.
    """


class OpensAndShutsThreads(ShutsThread, OpensThreads, KnowsItsServers, Protocol):
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
        self, *, tracked_item_id: int, thread_id: int, guild_id: int, the_block_pinged: bool
    ) -> tuple[str, ...]: ...


class ThreadBinding(Protocol):
    """Keeping one item pointed at one thread, whatever Discord does in between."""

    async def write(
        self,
        target: ThreadTarget,
        *,
        name: str,
        panel: Panel,
        replacement: Panel | None = None,
        notify: Notify = None,
    ) -> ThreadWrite:
        """`replacement` is what a thread opened to REPLACE one gets instead of `panel`.

        Two renderings of the same item, differing only in whether the people on it are live
        mentions. Which one a write needs is not known until the write is under way: an edit
        notifies nobody so it may carry them, a thread opened for the first time is meant to,
        and a thread opened because the old one was deleted or is in the wrong channel must not.
        Nothing about the item changed in that last case, and telling everybody on it that
        somebody tidied a channel is the ping this whole path exists to stop sending.

        `notify` is which of the people the block names may actually be notified, which is a
        separate question from whether they are named: somebody who ran `/mentions off` is still
        in the block, as a mention, and is left off this list.
        """
        ...


class ItemSyncService:
    """The one path a GitHub object takes into Discord.

    Pull requests and issues share this, and so do the webhook pipeline and the manual
    commands. What differs between object types lives in the policy, which is the only way two
    kinds of item can stay consistent without two copies of this.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: ShutsAndKnowsServers,
        policy: SyncPolicy,
        binding: ThreadBinding,
        notifier: Notifier | None = None,
        *,
        mentions: bool = True,
        notifies: bool = True,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._one_item = ItemLock(sessionmaker)
        self._threads = threads
        self._binding = binding
        self._policy = policy
        self._notifier = notifier
        # Whether the block may carry live mentions at all. Off for the paths that open threads
        # in bulk, so a backlog mirror names people in plain text and notifies nobody. Built with
        # it rather than told per call, the same as the notifier: there is nothing to turn on.
        self._mentions = mentions
        # Whether anybody the block names may actually be RUNG. Its own switch because the two
        # used to be one, and one caller needs them apart: a redraw names somebody linked since
        # the thread opened as a live mention, which is the whole point of it, and must ring
        # nobody doing so.
        #
        # Off means an empty allow-list, which Discord reads as "notify nobody". None would leave
        # the client's own rule in force instead, which is a different answer entirely.
        #
        # On the ordinary path this changes nothing, because the block is an edit and an edit
        # notifies nobody whatever it says. It is for the one path that POSTS the block instead:
        # a metadata message somebody deleted, which `_edit_or_post` replaces with a new message.
        self._notifies = notifies

    async def sync(
        self,
        snapshot: TrackedSnapshot,
        *,
        settles_the_lock: bool = True,
        arrived: int | None = None,
    ) -> SyncResult:
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

        async with self._one_item.held(snapshot.github_object_id):
            return await self._mirror(snapshot, settles_the_lock, arrived)

    async def _mirror(
        self, snapshot: TrackedSnapshot, settles_the_lock: bool, arrived: int | None
    ) -> SyncResult:
        """One sync of one item, with that item held to this one."""
        decision = await self._record(snapshot, arrived)
        # The database step either hands over work to do or answers on its own.
        if isinstance(decision, SyncResult):
            # The id is tested beside the outcome rather than trusted from it. Both answers of
            # STALE are built off a row that was read, so it is always set, and the two below
            # would no-op anyway on a None: one updates rows by an id that matches nothing, the
            # other reads a row that is not there and returns. The test costs nothing at runtime
            # and nothing in coverage, being the second half of a short-circuit that the stale
            # path already takes, and it says on the line what is otherwise only true two
            # methods away. The same reasoning put `thread_id is None` in the guard inside
            # `_settle_a_lock_still_owed`, where the comment says the case cannot happen either.
            if decision.outcome is SyncOutcome.STALE and decision.tracked_item_id is not None:
                await self._reopen_new_requests(decision.tracked_item_id, snapshot)
                await self._settle_a_lock_still_owed(decision.tracked_item_id, decision.thread_id)
            return decision
        state = decision

        try:
            return await self._show_it(state, snapshot, settles_the_lock)
        except PermanentError:
            # A refusal that looks like a permission, from a bot that may simply not be in the
            # server any more. discord.py empties the guild from its cache the moment it is
            # removed, and Discord answers for a channel it can no longer see with the same
            # refusal it gives for one it is not allowed to touch.
            #
            # Told apart because the two want opposite things. A missing permission is permanent
            # and is dropped on the first attempt, which is right: nobody grants a permission by
            # waiting. Being out of a server is minutes long, an admin removing the bot and
            # putting it back or re-authorising the integration, and the sixteen attempts over
            # two hours exist for exactly that. Dropped as permanent, every delivery in the
            # window was lost, and the row had already been committed, so the item was left
            # saying one thing while its thread said another with nothing coming to correct it.
            if self._threads.is_in(state.guild_id):
                raise
            raise DiscordGatewayError(
                f"this bot is not in server {state.guild_id} at the moment, so nothing about "
                f"{snapshot.repository.full_name}#{snapshot.number} could be written"
            ) from None

    async def _show_it(
        self, state: _SyncState, snapshot: TrackedSnapshot, settles_the_lock: bool
    ) -> SyncResult:
        """Everything this sync says to Discord, which is everything that can be refused.

        Its own method so the refusal above has something to wrap. Almost nothing here is
        decided again: the row was read and written by the step before it. The exception is the
        lock, which is the last thing said to Discord and therefore the one decision that can
        have been overtaken while the calls above it were being made, so it looks at the row
        once more before it commits to shutting anything.
        """
        # Discord is called outside the transaction. Holding one open across a network call
        # would let a slow gateway block the database, and a rollback would throw away work
        # Discord had already done.
        wants_shut = state.wants_shut

        # Unlocking comes first and locking last, so that everything in between happens on an
        # open thread. Not because the bot cannot write to a locked one, which it can and which
        # `test_a_locked_thread_still_accepts_metadata_updates` pins: because a reader arriving
        # mid-sync should never find a thread locked against a state it has not been given yet.
        # `settles_the_lock` gates this as well as the shut at the end, and the two are the same
        # promise read in both directions. A caller that says it will take the lock itself owns
        # both halves of it: `/set_done` and the commands beside it decide the status the lock
        # follows from and report a refusal to the person who ran them. Reaching in here to give
        # a thread back would take that refusal away from them, and worse, it fails the command
        # outright, because a transient refusal is not caught below. Nothing noticed until a
        # pull request started answering False on every open delivery; before that this branch
        # was reached only by an issue, and a command may not put an open issue anywhere.
        if settles_the_lock and wants_shut is False and state.thread_id is not None:
            # A thread that has been deleted is rebuilt by the write below, and a new thread is
            # never locked. Letting this raise instead would stop the rebuild ever running: an
            # open issue always unlocks, so a deleted thread would end its mirror for good.
            #
            # A permission nobody has granted is stepped over for a harder reason. This is the
            # first Discord call a delivery makes, so raising here loses everything after it:
            # the block is never rewritten, and because the refusal is permanent the worker drops
            # the delivery on its first attempt. A server that has never granted Manage Threads
            # would have every reopened issue stop mirroring entirely, rather than mirror with a
            # thread that stays shut. Locking has the same bargain and can afford it more easily,
            # being last.
            try:
                await self._threads.set_shut(thread_id=state.thread_id, shut=False)
            except ThreadNotFoundError:
                pass
            except PermanentError as refusal:
                logger.warning(
                    "could not give back the thread for tracked item %s, so it stays shut: %s",
                    state.tracked_item_id,
                    refusal,
                )
            else:
                await self._note_the_lock(state.tracked_item_id, state.thread_id, locked=False)

        written = await self._binding.write(
            state.target,
            name=state.thread_name,
            panel=state.metadata,
            replacement=state.quiet_metadata,
            notify=state.notify,
        )
        handle = written.handle

        if written.created:
            # Only a posted block. An edit is invisible from the channel, so a block rewritten by
            # a command has shown its reader nothing, and recording it would silence the line
            # that command's own webhook produces.
            await self._note_the_block(state.tracked_item_id, handle.thread_id, state.labels)

        notified: tuple[str, ...] = ()
        if self._notifier is not None:
            notified = await self._notifier.notify(
                tracked_item_id=state.tracked_item_id,
                thread_id=handle.thread_id,
                guild_id=state.guild_id,
                # A block reaches the people it names when it is POSTED as a new message, which
                # is a thread being opened for the first time. An edit notifies nobody, and a
                # thread opened to replace one carries the plain block on purpose.
                the_block_pinged=written.created and state.thread_id is None,
            )

        # Two reasons to shut it, kept apart because they are answered by different things.
        #
        # The payload asked for it, and the guard is about a payload that may have been
        # superseded while this sync was in Discord. A lock decided from the row has nothing to
        # be superseded by: the row is what a newer sync would have written, and it was read
        # after that sync committed or not at all.
        asked_for = wants_shut is True and (
            state.shut_from_the_row or await self._still_current(state.tracked_item_id, snapshot)
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

        # Carried back rather than logged and forgotten, because the closing header is written
        # after this and is the only place anybody will see it. A thread that could not be shut
        # says so there; the row still says it is open, so the next delivery tries again.
        shut_refused = False
        if asked_for or shut_by_the_row:
            shut_refused = await self._shut(
                state.tracked_item_id, handle.thread_id, guild_id=state.guild_id
            )

        return SyncResult(
            outcome=SyncOutcome.SYNCED,
            tracked_item_id=state.tracked_item_id,
            thread_id=handle.thread_id,
            message_id=handle.message_id,
            created=written.created,
            notified=notified,
            shut_refused=shut_refused,
            displaced=written.displaced,
        )

    async def _reopen_new_requests(self, tracked_item_id: int, snapshot: TrackedSnapshot) -> None:
        """Hand back the ping on a request this payload made, even where it is out of date.

        A request made again is the one fact that arrives on exactly one delivery and nowhere
        else. GitHub puts the team it has just asked at the top level of a single
        `review_requested` payload; every later payload carries the same unchanged list, so
        `replace` leaves the row alone and nothing in it says the ask happened twice.

        For a person there is a second route: their row is stamped when they review, and any
        later payload measured against that stamp reopens it. A team's row is never stamped,
        deliberately, because stamping it made the row look answered and reopened it once per
        review round for an ask nobody had made. So for a team the single delivery is the whole
        of it, and a delivery turned away as superseded loses the ask for the life of the pull
        request: the role is never told, and every later event finds the row exactly as it was.
        A person re-requested in the same breath is told, which is how this looks from Discord.

        Safe to do from a delivery that is out of date about everything else, because this one
        write is not decided from the payload's view of the world. It compares the moment the
        payload was made against the moment the row already holds and does nothing unless the
        first is later, both on GitHub's clock, which is what makes a replayed delivery harmless
        and makes an out-of-order one harmless for the same reason.

        The ping itself is left to whoever claims it next, which is the delivery this one was
        turned away for if it has not passed the notifier yet, and otherwise the next event on
        the item. Sending it here would mean a superseded delivery posting to Discord, and the
        thing it would be posting is owed either way.
        """
        asked = self._policy.asked_again(snapshot)
        if not any(actors for actors in asked.values()):
            return
        async with self._sessionmaker() as session, session.begin():
            assignments = ItemAssignmentStore(session)
            for role, actors in asked.items():
                reopened = await assignments.reopen_request(
                    tracked_item_id, role, [actor.login for actor in actors], snapshot.updated_at
                )
                if reopened:
                    logger.info(
                        "review requested again from %s, on a delivery that was out of date "
                        "about everything else",
                        ", ".join(reopened),
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
            found = await TrackedItemStore(session).get_with_its_server(tracked_item_id)
        # The thread cannot be missing here, because both answers of STALE require the item to
        # have one, but it is checked with the rest rather than trusted: the cost of being wrong
        # about that is a Discord call made against nothing.
        if found is None or thread_id is None or found[0].discord_thread_locked is True:
            return
        item, guild_id = found
        if not self._policy.shut_for_state(status=item.status, github_state=item.github_state):
            return
        await self._shut(tracked_item_id, thread_id, guild_id=guild_id)

    async def _shut(self, tracked_item_id: int, thread_id: int, *, guild_id: int) -> bool:
        """Close the thread, and write down that it is closed. True if a permission refused it.

        Writing it down is what makes a second attempt possible. The lock used to be asked for
        only on the delivery attempt that opened the thread, and that fact lives for one attempt:
        anything failing after the thread was claimed onto the row and before the lock landed
        left the retry finding a thread already there, asking for nothing, and recording the
        delivery handled. A finished pull request kept a thread anybody could post in above a
        block reading DONE, and nothing revisited it.

        A permission this bot has never been granted is a different answer from a bad moment.
        Failing the delivery over one would drop it on the first attempt, and every later event
        for the item would drop the same way, for a thread nobody can shut until somebody grants
        the permission. It is said and stepped over instead, with the row left saying the thread
        is not shut, so granting it later is enough on its own. A bad moment still fails the
        delivery, because that is what gets it another go.

        A payload that asked used to fail on either, on the grounds that dropping it silently
        would leave a closed issue looking open with nothing recorded anywhere. Right about the
        silence and wrong about the answer: the drop took the metadata write and the closing
        header down with it, so the thread said nothing at all rather than saying the wrong
        thing. The refusal is carried back instead, and the header says the item closed and the
        thread could not. Extending the old rule was not an option either way, because closing a
        pull request now asks, and a server without the permission would have lost every one.
        """
        try:
            await self._threads.set_shut(thread_id=thread_id, shut=True)
        except PermanentError as refusal:
            # Stepping over a refusal is right for a permission nobody has granted and wrong for
            # a server this bot is no longer in, and Discord answers both the same way. The
            # difference matters more here than anywhere: a delivery that reaches this step is
            # reported handled either way, and both callers of this reach it for an item that is
            # finished, which sends no further event. Stepped over while out of the server, the
            # thread stays open above a block reading DONE and nothing is ever coming back for
            # it. The refusal that is really an absence fails the delivery instead, which is
            # what gets it another go once the bot is back.
            if not self._threads.is_in(guild_id):
                raise DiscordGatewayError(
                    f"this bot is not in server {guild_id} at the moment, so the thread for "
                    f"tracked item {tracked_item_id} could not be shut"
                ) from None
            logger.warning(
                "could not shut the thread for tracked item %s: %s", tracked_item_id, refusal
            )
            return True
        await self._note_the_lock(tracked_item_id, thread_id, locked=True)
        return False

    async def _note_the_block(
        self, tracked_item_id: int, thread_id: int, shown: tuple[str, ...]
    ) -> None:
        """Its own transaction, because the Discord call it records happens outside one."""
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).note_shown_labels(
                tracked_item_id, thread_id=thread_id, shown=shown
            )

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

    async def _record(
        self, snapshot: TrackedSnapshot, arrived: int | None = None
    ) -> _SyncState | SyncResult:
        """The database half, in one transaction: work out where this goes, then write it.

        Split in two because the two halves fail differently. Resolving can decide there is
        nothing to do at all, and writing cannot.
        """
        async with self._sessionmaker() as session, session.begin():
            placement = await self._resolve(session, snapshot, arrived)
            if isinstance(placement, SyncResult):
                return placement
            return await self._write(session, snapshot, placement, arrived)

    async def _resolve(
        self, session: AsyncSession, snapshot: TrackedSnapshot, arrived: int | None = None
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
        superseded = item is not None and is_superseded(
            snapshot.updated_at,
            item.github_updated_at,
            arrived=arrived,
            applied=item.last_delivery_id,
        )

        # `item is not None` is what `superseded` already implies; it is written out because the
        # checker cannot carry that through the local, and everything read below is the row.
        if item is not None and superseded and item.discord_thread_id is not None:
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
        self,
        session: AsyncSession,
        snapshot: TrackedSnapshot,
        placement: _Placement,
        arrived: int | None = None,
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
            superseded = is_superseded(
                snapshot.updated_at,
                item.github_updated_at,
                arrived=arrived,
                applied=item.last_delivery_id,
            )
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
                private=snapshot.repository.private,
            )

        roles: Mapping[ActorRole, Sequence[Actor]] = self._policy.assignments(snapshot)
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
            # Which delivery this was, so the next one carrying the same second can be placed
            # against it. Only on the branch that believes the payload: a rebuild from an old
            # delivery is deliberately not adopting anything it says, and recording its number
            # would tell the guard that an older delivery is the newest thing applied.
            if arrived is not None:
                item.last_delivery_id = arrived

        # People only. A team slug and a GitHub login are separate namespaces on GitHub's side,
        # so somebody may genuinely hold the user account `security` while a team of that name
        # also exists, and proving one says nothing about the other. Before issue #144 a member
        # could simply claim the name; now they can hold it, and the account map still cannot
        # tell a person from a team.
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
        # Off wholesale for `/refresh`, which opens threads for a whole backlog at once. Every
        # one of those is a first open, so every one of them would be a real message full of
        # live mentions, and a run over twenty-five items would notify everybody on all of them
        # about nothing that happened.
        mentions: Mapping[str, int] = {}
        if self._mentions:
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
            snapshot.corrected(title=item.title, state=item.github_state, html_url=item.github_url)
            if superseded
            else snapshot
        )
        # Who, of the people this block is about to name, has not asked to be left alone. Sent
        # with the write rather than folded into the rendering, because the two answer different
        # questions: the block still shows a muted person as a mention, so the thread records who
        # is on the item, and Discord is told separately not to ring them.
        #
        # Always a tuple and never None. An empty allow-list tells Discord to notify nobody,
        # where None would leave the client's own rule in force.
        #
        # It comes back empty two ways. A path built without mentions resolves nobody, so there
        # is nothing to ask about, which is what makes a backlog mirror silent twice over off one
        # switch rather than by the rendering alone. And a path built without `notifies` does not
        # ask at all, which is how a block can name people as live mentions and still ring none
        # of them.
        notify: tuple[int, ...] = ()
        if self._notifies:
            notify = await MutedMemberStore(session).may_be_pinged(
                guild_id=placement.repository.discord_guild_id, ids=mentions.values()
            )

        metadata = self._policy.render(
            shown, status=item.status, priority=item.priority, mentions=mentions
        )
        return _SyncState(
            tracked_item_id=item.id,
            guild_id=placement.repository.discord_guild_id,
            channel_id=placement.channel_id,
            thread_id=item.discord_thread_id,
            message_id=item.discord_message_id,
            metadata=metadata,
            # The same block with the people named in plain text, for a thread opened to replace
            # one. Rendered rather than branched on up here because whether this write replaces
            # anything is decided inside the binding, after a Discord call has already failed.
            # Identical to the one above wherever there was nobody to mention, which is most
            # items and every path built without mentions at all.
            quiet_metadata=(
                metadata
                if not mentions
                else self._policy.render(
                    shown, status=item.status, priority=item.priority, mentions={}
                )
            ),
            thread_name=self._policy.thread_name(shown),
            wants_shut=self._policy.shut(shown, status=item.status),
            shut_from_the_row=superseded,
            shut_when_opened=item.status is Status.DONE,
            thread_locked=item.discord_thread_locked,
            thread_channel_id=item.discord_channel_id,
            labels=tuple(shown.label_names),
            notify=notify,
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
    sessionmaker: async_sessionmaker[AsyncSession],
    threads: OpensAndShutsThreads,
    policy: SyncPolicy,
    notifier: Notifier | None = None,
    *,
    relocates: bool = False,
    mentions: bool = True,
    notifies: bool = True,
) -> ItemSyncService:
    """Assemble a sync service and the thread binding it drives.

    The service locks threads and nothing else, so its constructor asks for nothing else. The
    binding is what opens and rewrites them, and it needs a wider handle; composing the two is
    this function's whole job.

    `relocates` off means a thread in the wrong channel is written to where it is, which is what
    every delivery wants. Only the wiring behind `/set_channel` turns it on.
    """
    return ItemSyncService(
        sessionmaker,
        threads,
        policy,
        ItemThreads(sessionmaker, threads, relocates=relocates),
        notifier,
        mentions=mentions,
        notifies=notifies,
    )


def build_item_handler(
    service: SyncsItems,
    parse: SnapshotParser,
    *,
    announce: AnnouncesInThread | None = None,
) -> EventHandler:
    """Adapt a webhook event to the sync service.

    Only the parser differs between object types, so both kinds of event share this rather
    than each having its own near-identical handler module.

    `announce` is optional and off by default, the same shape as the note handler's `then`: a
    caller that only wants an item mirrored should not have to know anything is announced, and
    every test that builds this without one gets the behaviour it was written against.

    One announcer, and this module knows nothing about what it says. Each reads the delivery and
    decides for itself, so composing several is the wiring's business, the way the two notifiers
    are already composed there. What used to be in this function was the label parse and its
    null check, and both went with it.
    """

    async def handle(
        action: str, payload: JsonObject, arrived: int | None = None
    ) -> WebhookOutcome:
        snapshot = parse(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED
        result = await service.sync(snapshot, arrived=arrived)

        # After the sync, and only after it, because a line belongs under a block that says what
        # the item is now.
        #
        # `thread_id` rather than `result.synced`, because a delivery that is turned away as
        # superseded still has a thread to say something in, and a retry of a delivery whose
        # line was never posted is exactly that case. An announcer that wants a narrower answer
        # than that asks its own question.
        #
        # `tracked_item_id` is in the same test to narrow it rather than because it happens: the
        # two are written together everywhere a thread is recorded, and nothing has one without
        # the other.
        if (
            announce is not None
            and arrived is not None
            and result.thread_id is not None
            and result.tracked_item_id is not None
        ):
            await announce.say(
                Arrival(
                    action=action,
                    snapshot=snapshot,
                    payload=payload,
                    tracked_item_id=result.tracked_item_id,
                    thread_id=result.thread_id,
                    arrived=arrived,
                    shut_refused=result.shut_refused,
                )
            )

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
    metadata: Panel
    # The thread's name, rendered beside the block it belongs with rather than in the caller,
    # so the two cannot end up describing different states.
    thread_name: str
    thread_id: int | None
    message_id: int | None
    # Whether the thread belongs shut, or None for a case this path does not touch: a ticket,
    # always, and an open pull request somebody put at DONE by hand. Decided in `_write`, where
    # the payload and the row are both in hand, because on the rebuild path they disagree and
    # only one of them is to be believed.
    wants_shut: bool | None
    # Whether that answer came from the row rather than from the payload, which decides whether
    # the staleness guard below applies to it at all.
    shut_from_the_row: bool
    # Whether a thread opened by this sync belongs shut, read off the row's status. Only for the
    # case the payload says nothing about, which is now `/set_done` on an open pull request.
    shut_when_opened: bool
    # What the row remembers this bot last making of the thread it points at. Null means it has
    # not shut one, which is what a thread just opened is and what every row written before the
    # column existed says.
    thread_locked: bool | None
    # Where the row says that thread actually is, which is not where the mapping says new ones
    # go the moment anybody has run `/set_channel`.
    thread_channel_id: int | None
    # The label names the block about to be written carries, recorded against the item when that
    # block is POSTED. What a reader has been shown, which the item's own labels cannot answer.
    labels: tuple[str, ...]
    # The same block with nobody mentioned, for a thread that replaces one. See `ThreadBinding`.
    quiet_metadata: Panel
    # Which of the people the block names this bot may notify. Empty where it names nobody, which
    # is never the same answer as having no opinion. See `Notify`.
    notify: tuple[int, ...]

    @property
    def target(self) -> ThreadTarget:
        return ThreadTarget(
            tracked_item_id=self.tracked_item_id,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            message_id=self.message_id,
            thread_channel_id=self.thread_channel_id,
        )
