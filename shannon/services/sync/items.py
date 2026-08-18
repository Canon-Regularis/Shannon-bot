from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.errors import ThreadNotFoundError
from shannon.discord_bot.threads import LocksThread, OpensThreads
from shannon.domain.enums import ActorRole, Priority, Status
from shannon.domain.errors import WrongPolicyError
from shannon.domain.models import Actor, TrackedSnapshot
from shannon.domain.time import as_utc
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


class SyncThreads(LocksThread, OpensThreads, Protocol):
    """Two roles, because this service does one of them and delegates the other.

    Locking is the last step of a sync and belongs here. Opening, rewriting and removing belong
    to the binding, which this service builds by default and therefore has to be handed the
    means to. Posting is nobody's business here, which is why it is absent.
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
        threads: SyncThreads,
        policy: SyncPolicy,
        notifier: Notifier | None = None,
        *,
        binding: ThreadBinding | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        # Built here by default because it needs exactly what this service already has, and
        # nothing has ever wanted a different one. The parameter exists so the seam is visible
        # rather than buried, and so a caller that does want another can say so.
        self._binding = binding or ItemThreads(sessionmaker, threads)
        self._policy = policy
        self._notifier = notifier

    async def sync(self, snapshot: TrackedSnapshot) -> SyncResult:
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
            return decision
        state = decision

        # Discord is called outside the transaction. Holding one open across a network call
        # would let a slow gateway block the database, and a rollback would throw away work
        # Discord had already done.
        wants_locked = self._policy.locked(snapshot)

        # Unlocking comes first, because a locked thread rejects edits and posts. Locking comes
        # last for the same reason, by which point the thread is known to exist.
        if wants_locked is False and state.thread_id is not None:
            # A thread that has been deleted is rebuilt by the write below, and a new thread is
            # never locked. Letting this raise instead would stop the rebuild ever running: an
            # open issue always unlocks, so a deleted thread would end its mirror for good.
            with contextlib.suppress(ThreadNotFoundError):
                await self._threads.set_locked(thread_id=state.thread_id, locked=False)

        written = await self._binding.write(
            state.target, name=self._policy.thread_name(snapshot), content=state.metadata
        )
        handle = written.handle

        notified: tuple[str, ...] = ()
        if self._notifier is not None:
            notified = await self._notifier.notify(
                tracked_item_id=state.tracked_item_id,
                thread_id=handle.thread_id,
                guild_id=state.guild_id,
            )

        if wants_locked is True and await self._still_current(state.tracked_item_id, snapshot):
            await self._threads.set_locked(thread_id=handle.thread_id, locked=True)

        return SyncResult(
            outcome=SyncOutcome.SYNCED,
            tracked_item_id=state.tracked_item_id,
            thread_id=handle.thread_id,
            message_id=handle.message_id,
            created=written.created,
            notified=notified,
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
        if snapshot.updated_at is None:
            return True

        async with self._sessionmaker() as session:
            item = await TrackedItemStore(session).get_by_id(tracked_item_id)
            stored = item.github_updated_at if item is not None else None

        if stored is None or as_utc(stored) <= as_utc(snapshot.updated_at):
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

        channel = await ChannelMappingStore(session).resolve(repository.id, object_type)
        if channel is None:
            logger.warning(
                "%s has no channel mapped for %s, run /set_channel",
                snapshot.repository.full_name,
                object_type.value,
            )
            return SyncResult(outcome=SyncOutcome.NOT_TRACKED)

        item = await TrackedItemStore(session).get(
            repository_id=repository.id,
            object_type=object_type,
            github_object_id=snapshot.github_object_id,
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
    ) -> _SyncState:
        """Bring the stored item in line with the snapshot, and render what Discord will show."""
        object_type = self._policy.object_type
        repositories = RepositoryStore(session)
        items = TrackedItemStore(session)

        # Only once the delivery is known to be current. Every payload carries the repository's
        # name at the time it was sent, so following one from a delivery that arrived late would
        # put the old name back and break /pr all over again.
        await repositories.follow_rename(
            placement.repository,
            repo_name=snapshot.repository.full_name,
            repo_url=snapshot.repository.html_url,
        )

        item = placement.item
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
                priority=self._policy.priority_for(snapshot, Priority.UNSET),
                github_updated_at=snapshot.updated_at,
            )

        roles = self._policy.assignments(snapshot)
        if placement.superseded:
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
            await self._store_people(session, item.id, roles, as_of=snapshot.updated_at)

        logins = [actor.login for actors in roles.values() for actor in actors]
        mentions = await UserLinkStore(session).resolve_many(
            guild_id=placement.repository.discord_guild_id, github_usernames=logins
        )

        return _SyncState(
            tracked_item_id=item.id,
            guild_id=placement.repository.discord_guild_id,
            channel_id=placement.channel_id,
            thread_id=item.discord_thread_id,
            message_id=item.discord_message_id,
            metadata=self._policy.render(
                snapshot, status=item.status, priority=item.priority, mentions=mentions
            ),
        )

    def _apply(self, items: TrackedItemStore, item: TrackedItem, snapshot: TrackedSnapshot) -> None:
        item.title = snapshot.title
        item.github_url = snapshot.html_url
        item.github_object_number = snapshot.number
        item.github_state = snapshot.display_state
        item.status = self._policy.status_for(snapshot, item.status)
        item.priority = self._policy.priority_for(snapshot, item.priority)
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
        *,
        as_of: datetime | None,
    ) -> None:
        """Make the stored people match the payload, and reopen anything it asks for again.

        `as_of` is when GitHub says this payload was current. A request already closed by a
        review is only reopened by a payload newer than that review, which is what separates
        somebody clicking re-request from a delivery that has been retrying since before it.
        """
        assignments = ItemAssignmentStore(session)
        for role, actors in roles.items():
            await assignments.replace(tracked_item_id=tracked_item_id, role=role, actors=actors)
            reopened = await assignments.reopen_if_newer(
                tracked_item_id, role, [actor.login for actor in actors], as_of
            )
            if reopened:
                logger.info("review requested again from %s", ", ".join(reopened))


def build_item_handler(service: ItemSyncService, parse: SnapshotParser) -> EventHandler:
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
    thread_id: int | None
    message_id: int | None

    @property
    def target(self) -> ThreadTarget:
        return ThreadTarget(
            tracked_item_id=self.tracked_item_id,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            message_id=self.message_id,
        )
