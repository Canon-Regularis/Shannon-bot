from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import ThreadGateway
from shannon.domain.models import ItemNote
from shannon.github.webhooks.events import EventHandler, WebhookOutcome

logger = logging.getLogger(__name__)

Renderer = Callable[[Any, Mapping[str, int]], str]
NoteParser = Callable[[str, Mapping[str, Any]], ItemNote | None]


class ItemNoteMirror:
    """Posts comments and reviews into the thread of whatever they were left on.

    Finding the thread is the same work for both, and for pull requests and issues alike, so
    only the rendering is injected.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: ThreadGateway,
        *,
        render: Renderer,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._render = render

    async def mirror(self, snapshot: ItemNote) -> bool:
        """Post the note, returning whether there was anywhere to post it."""
        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_github_id(
                snapshot.repository.github_repo_id
            )
            if repository is None:
                return False

            # By number, not by id. A pull request reports its issue id in comment payloads,
            # which never matches the pull request id stored against the tracked item.
            item = await TrackedItemStore(session).get_by_number(
                repository_id=repository.id,
                number=snapshot.item_number,
                object_type=snapshot.object_type,
            )
            if item is None or item.discord_thread_id is None:
                logger.debug(
                    "note on %s#%s is not tracked here, ignoring",
                    snapshot.repository.full_name,
                    snapshot.item_number,
                )
                return False

            thread_id = item.discord_thread_id
            mentions = (
                await UserLinkStore(session).resolve_many(
                    guild_id=repository.discord_guild_id,
                    github_usernames=[snapshot.author.login],
                )
                if snapshot.author
                else {}
            )

        await self._threads.post(thread_id=thread_id, content=self._render(snapshot, mentions))
        logger.info("mirrored a note on %s#%s", snapshot.repository.full_name, snapshot.item_number)
        return True


def build_note_handler(mirror: ItemNoteMirror, parse: NoteParser) -> EventHandler:
    """Adapt a comment or review webhook to the mirror."""

    async def handle(action: str, payload: Mapping[str, Any]) -> WebhookOutcome:
        snapshot = parse(action, payload)
        if snapshot is None:
            return WebhookOutcome.IGNORED
        posted = await mirror.mirror(snapshot)
        return WebhookOutcome.PROCESSED if posted else WebhookOutcome.IGNORED

    return handle
