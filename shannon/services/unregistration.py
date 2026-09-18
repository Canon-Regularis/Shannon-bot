"""Undoing the binding between a repository and a Discord server.

Registering has been one way since it was written. That was survivable while the worst case was a
public repository mirrored into the wrong channel; it is not survivable for private code, where
the binding decides where issue titles and comment bodies are posted.

What makes this safe to offer is not the Discord role. It is that the caller has proved to GitHub
that they hold admin on the repository, which is the one thing a role cannot establish and `/link`
cannot either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.errors import (
    NotProvenError,
    NotRegisteredError,
    RepositoryMismatchError,
)

logger = logging.getLogger(__name__)

# The only permission that may unbind. GitHub folds `maintain` into `write` and `triage` into
# `read` before answering, so this is the whole of the admin tier and nothing else comes close
# enough to be worth arguing about.
ADMIN = "admin"


class ReadsPermissions(Protocol):
    """What one GitHub account may do to one repository.

    Its own protocol because it is all this service needs, and because it is the only question
    asked of GitHub anywhere on the unregister path. A service that could destroy a binding should
    not be holding a handle that can also write a label.
    """

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...


@dataclass(frozen=True, slots=True)
class UnregisterOutcome:
    """What was unbound, so the reply can say what has just been lost."""

    full_name: str
    threads_orphaned: int


class RepositoryUnregistrationService:
    """Unbinds a repository from a server, once somebody has proved they may."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], github: ReadsPermissions
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github

    async def unregister(self, *, guild_id: int, full_name: str, login: str) -> UnregisterOutcome:
        """Check the caller holds admin on the bound repository, then unbind it.

        `full_name` is typed out by whoever ran the command and is checked against what is
        actually registered. It is a confirmation rather than a lookup: this is irreversible and
        it cascades, so the cheapest guard available is making somebody name the thing.

        `login` must be a login GitHub itself vouched for a moment ago. Passing one out of
        `user_links` would make the whole check theatre, because anybody with the Admin role in
        the server can write whatever they like into that table.
        """
        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
        if stored is None:
            raise NotRegisteredError("This server has no repository registered.")

        if stored.repo_name.casefold() != full_name.strip().casefold():
            raise RepositoryMismatchError(
                f"This server is registered to {stored.repo_name}, not {full_name.strip()}. "
                "Run /unregister with the repository's full name to confirm."
            )

        owner, _, name = stored.repo_name.partition("/")
        permission = await self._github.permission_for(owner, name, login)
        if permission != ADMIN:
            logger.info(
                "refusing to unregister %s for github:%s, who has %r",
                stored.repo_name,
                login,
                permission,
            )
            raise NotProvenError(
                f"You are signed in as {login}, who does not have admin on {stored.repo_name}. "
                "Only somebody who can administer the repository can unregister it."
            )

        orphaned = await self._unbind(stored.id)
        logger.info(
            "unregistered %s from guild %s at the request of github:%s",
            stored.repo_name,
            guild_id,
            login,
        )
        return UnregisterOutcome(full_name=stored.repo_name, threads_orphaned=orphaned)

    async def _unbind(self, repository_id: int) -> int:
        """Delete the binding, counting the threads it leaves behind.

        Counted before the delete rather than after, because the rows are gone afterwards: the
        foreign keys cascade from `repositories` through `channel_mappings` and `tracked_items`
        and on to the assignments and mirrored notes.

        The Discord threads themselves are untouched and stay in the channel. Nothing here deletes
        them, both because that is a great deal of destruction to do on one command and because
        the history in them is usually why somebody wants the repository unbound rather than the
        channel emptied.
        """
        async with self._sessionmaker() as session, session.begin():
            orphaned = await session.scalar(
                select(func.count())
                .select_from(TrackedItem)
                .where(TrackedItem.repository_id == repository_id)
            )
            await session.execute(delete(Repository).where(Repository.id == repository_id))
        return orphaned or 0
