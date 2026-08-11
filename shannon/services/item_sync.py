from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole, Priority, Status
from shannon.domain.models import Actor, TrackedSnapshot
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.services.notifications import ActorNotifier
from shannon.services.policies import SyncPolicy
from shannon.services.staleness import is_stale

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


class ItemSyncService:
    """The one path a GitHub object takes into Discord.

    Pull requests and issues share this, and so do the webhook pipeline and the manual
    commands. What differs between object types lives in the policy, which is the only way two
    kinds of item can stay consistent without two copies of this.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: ThreadGateway,
        policy: SyncPolicy,
        notifier: ActorNotifier | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._policy = policy
        self._notifier = notifier

    async def sync(self, snapshot: TrackedSnapshot) -> SyncResult:
        """Bring Discord in line with a snapshot."""
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
        # last for the same reason.
        if wants_locked is False and state.thread_id is not None:
            await self._threads.set_locked(thread_id=state.thread_id, locked=False)

        handle, created = await self._write_thread(state, self._policy.thread_name(snapshot))
        await self._persist_thread(state.tracked_item_id, handle.thread_id, handle.message_id)

        notified: tuple[str, ...] = ()
        if self._notifier is not None:
            notified = await self._notifier.notify(
                tracked_item_id=state.tracked_item_id,
                thread_id=handle.thread_id,
                guild_id=state.guild_id,
            )

        if wants_locked is True:
            await self._threads.set_locked(thread_id=handle.thread_id, locked=True)

        return SyncResult(
            outcome=SyncOutcome.SYNCED,
            tracked_item_id=state.tracked_item_id,
            thread_id=handle.thread_id,
            message_id=handle.message_id,
            created=created,
            notified=notified,
        )

    async def _write_thread(self, state: _SyncState, name: str):
        if state.thread_id is None:
            handle = await self._threads.create(
                channel_id=state.channel_id, name=name, content=state.metadata
            )
            return handle, True

        handle = await self._threads.update(
            thread_id=state.thread_id,
            message_id=state.message_id,
            name=name,
            content=state.metadata,
        )
        return handle, False

    async def _record(self, snapshot: TrackedSnapshot) -> _SyncState | SyncResult:
        object_type = self._policy.object_type

        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                logger.debug(
                    "%s is not registered to any guild, ignoring", snapshot.repository.full_name
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

            items = TrackedItemStore(session)
            item = await items.get(
                repository_id=repository.id,
                object_type=object_type,
                github_object_id=snapshot.github_object_id,
            )

            if item is not None and is_stale(
                has_thread=item.discord_thread_id is not None,
                incoming=snapshot.updated_at,
                stored=item.github_updated_at,
            ):
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

            if item is None:
                item = await items.create(
                    repository_id=repository.id,
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
            self._apply(item, snapshot)

            roles = self._policy.assignments(snapshot)
            await self._store_people(session, item.id, roles)

            logins = [actor.login for actors in roles.values() for actor in actors]
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=repository.discord_guild_id, github_usernames=logins
            )

            return _SyncState(
                tracked_item_id=item.id,
                guild_id=repository.discord_guild_id,
                channel_id=channel.discord_channel_id,
                thread_id=item.discord_thread_id,
                message_id=item.discord_message_id,
                metadata=self._policy.render(
                    snapshot, status=item.status, priority=item.priority, mentions=mentions
                ),
            )

    def _apply(self, item: TrackedItem, snapshot: TrackedSnapshot) -> None:
        item.title = snapshot.title
        item.github_url = snapshot.html_url
        item.github_object_number = snapshot.number
        item.github_state = snapshot.display_state
        item.status = self._policy.status_for(snapshot, item.status)
        item.priority = self._policy.priority_for(snapshot, item.priority)
        if snapshot.updated_at is not None:
            item.github_updated_at = snapshot.updated_at

    async def _store_people(
        self,
        session: AsyncSession,
        tracked_item_id: int,
        roles: Mapping[ActorRole, Sequence[Actor]],
    ) -> None:
        assignments = ItemAssignmentStore(session)
        for role, actors in roles.items():
            await assignments.replace(tracked_item_id=tracked_item_id, role=role, actors=actors)

    async def _persist_thread(
        self, tracked_item_id: int, thread_id: int, message_id: int | None
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            items = TrackedItemStore(session)
            item = await items.get_by_id(tracked_item_id)
            if item is None:
                return
            await items.set_discord_ids(item, thread_id=thread_id, message_id=message_id)


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
class _SyncState:
    """Work for the Discord step, only ever built when there is work to do."""

    tracked_item_id: int
    guild_id: int
    channel_id: int
    metadata: str
    thread_id: int | None
    message_id: int | None
