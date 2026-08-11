from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import TrackedItem
from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.formatting import format_pull_request, pull_request_thread_name
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.enums import ActorRole, ObjectType, Status
from shannon.domain.models import PullRequestSnapshot
from shannon.github.webhooks.events import EventHandler, WebhookOutcome
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.notifications import ReviewerNotifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    tracked_item_id: int
    thread_id: int
    message_id: int | None
    created: bool
    notified_reviewers: tuple[str, ...] = ()


class PullRequestSyncService:
    """The one path a pull request takes into Discord.

    Both the webhook pipeline and `/pr` call `sync`, so a manually synced PR and a webhook
    synced PR cannot drift apart.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: ThreadGateway,
        notifier: ReviewerNotifier | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._notifier = notifier

    async def sync(self, snapshot: PullRequestSnapshot) -> SyncResult | None:
        """Bring Discord in line with a pull request snapshot.

        Returns None when the repository is not registered anywhere, which is the normal
        outcome for a webhook from a repository this bot was never pointed at.
        """
        state = await self._record(snapshot)
        if state is None:
            return None

        # Discord is called outside the transaction. A slow or failing gateway would otherwise
        # hold a database transaction open, and a rollback would throw away work that Discord
        # had already done.
        if state.thread_id is None:
            handle = await self._threads.create(
                channel_id=state.channel_id,
                name=pull_request_thread_name(snapshot),
                content=state.metadata,
            )
            created = True
        else:
            handle = await self._threads.update(
                thread_id=state.thread_id,
                message_id=state.message_id,
                name=pull_request_thread_name(snapshot),
                content=state.metadata,
            )
            created = False

        await self._persist_thread(state.tracked_item_id, handle.thread_id, handle.message_id)

        notified: tuple[str, ...] = ()
        if self._notifier is not None:
            notified = await self._notifier.notify(
                tracked_item_id=state.tracked_item_id,
                thread_id=handle.thread_id,
                guild_id=state.guild_id,
            )

        return SyncResult(
            tracked_item_id=state.tracked_item_id,
            thread_id=handle.thread_id,
            message_id=handle.message_id,
            created=created,
            notified_reviewers=notified,
        )

    async def _record(self, snapshot: PullRequestSnapshot) -> _SyncState | None:
        async with self._sessionmaker() as session, session.begin():
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                logger.debug(
                    "%s is not registered to any guild, ignoring", snapshot.repository.full_name
                )
                return None

            channel = await ChannelMappingStore(session).get(repository.id, ObjectType.PR)
            if channel is None:
                logger.warning(
                    "%s has no pull request channel mapping", snapshot.repository.full_name
                )
                return None

            items = TrackedItemStore(session)
            item = await items.get(
                repository_id=repository.id,
                object_type=ObjectType.PR,
                github_object_id=snapshot.github_object_id,
            )
            if item is None:
                item = await items.create(
                    repository_id=repository.id,
                    object_type=ObjectType.PR,
                    github_object_id=snapshot.github_object_id,
                    github_object_number=snapshot.number,
                    github_url=snapshot.html_url,
                    title=snapshot.title,
                    github_state=snapshot.display_state,
                    status=Status.NOT_REVIEWED,
                    github_updated_at=snapshot.updated_at,
                )
            else:
                _apply(item, snapshot)

            await self._store_people(session, item.id, snapshot)
            mentions = await UserLinkStore(session).resolve_many(
                guild_id=repository.discord_guild_id,
                github_usernames=_everyone(snapshot),
            )

            return _SyncState(
                tracked_item_id=item.id,
                guild_id=repository.discord_guild_id,
                channel_id=channel.discord_channel_id,
                thread_id=item.discord_thread_id,
                message_id=item.discord_message_id,
                metadata=format_pull_request(
                    snapshot, status=item.status, priority=item.priority, mentions=mentions
                ),
            )

    async def _store_people(
        self, session: AsyncSession, tracked_item_id: int, snapshot: PullRequestSnapshot
    ) -> None:
        assignments = ItemAssignmentStore(session)
        await assignments.replace(
            tracked_item_id=tracked_item_id,
            role=ActorRole.AUTHOR,
            actors=[snapshot.author] if snapshot.author else [],
        )
        await assignments.replace(
            tracked_item_id=tracked_item_id,
            role=ActorRole.ASSIGNEE,
            actors=snapshot.assignees,
        )
        await assignments.replace(
            tracked_item_id=tracked_item_id,
            role=ActorRole.REVIEWER,
            actors=snapshot.reviewers,
        )

    async def _persist_thread(
        self, tracked_item_id: int, thread_id: int, message_id: int | None
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            items = TrackedItemStore(session)
            item = await items.get_by_id(tracked_item_id)
            if item is None:
                return
            await items.set_discord_ids(item, thread_id=thread_id, message_id=message_id)


@dataclass(frozen=True, slots=True)
class _SyncState:
    """What the database step hands to the Discord step."""

    tracked_item_id: int
    guild_id: int
    channel_id: int
    thread_id: int | None
    message_id: int | None
    metadata: str


def _everyone(snapshot: PullRequestSnapshot) -> list[str]:
    actors = [*snapshot.assignees, *snapshot.reviewers]
    if snapshot.author is not None:
        actors.append(snapshot.author)
    return [actor.login for actor in actors]


def _apply(item: TrackedItem, snapshot: PullRequestSnapshot) -> None:
    item.title = snapshot.title
    item.github_url = snapshot.html_url
    item.github_object_number = snapshot.number
    item.github_state = snapshot.display_state
    if snapshot.updated_at is not None:
        item.github_updated_at = snapshot.updated_at


def build_pull_request_handler(service: PullRequestSyncService) -> EventHandler:
    """Adapt the sync service to the webhook router's handler shape."""

    async def handle(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        snapshot = parse_pull_request_event(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED
        result = await service.sync(snapshot)
        return WebhookOutcome.PROCESSED if result is not None else WebhookOutcome.IGNORED

    return handle
