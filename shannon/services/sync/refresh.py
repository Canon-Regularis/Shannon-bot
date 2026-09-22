"""Mirroring the open items that nothing ever opened a thread for.

The bot learns about an item when a webhook arrives for it, so anything open before the
repository was registered, or open through a gap in delivery, has no thread and never will.
Nothing here pings: the sync services it is handed are built without a notifier, in
`container._refresh`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError, ShannonError
from shannon.domain.models import RepositorySnapshot, TrackedSnapshot
from shannon.github.client import ListsOpenItems
from shannon.services.sync.items import SyncOutcome, SyncsItems
from shannon.services.sync.manual import SyncFailedError

logger = logging.getLogger(__name__)

# How many items one run will mirror. Fixed by Discord, not by the deployment: a command has
# fifteen minutes after it defers and each item costs two Discord calls, the thread and the
# message in it, so a larger cap risks finishing the work after the token has expired and the
# reply can no longer be delivered.
MIRRORED_PER_RUN = 25


class RefreshScope(StrEnum):
    """Which kinds one run covers."""

    EVERYTHING = "everything"
    PULL_REQUESTS = "pull_requests"
    ISSUES = "issues"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What a run did, in numbers, for the command to turn into a sentence.

    `failed` is counted inside `left`: an item this run could not mirror is still untracked, and
    a later run will try it again.
    """

    full_name: str
    mirrored: int
    already: int
    failed: int
    left: int


class RepositoryRefresh:
    """Mirror every open item on the registered repository that has no thread."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: ListsOpenItems,
        *,
        pull_requests: SyncsItems,
        issues: SyncsItems,
        cap: int = MIRRORED_PER_RUN,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._syncs = {ObjectType.PR: pull_requests, ObjectType.ISSUE: issues}
        self._cap = cap

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
        """Read the backlog and mirror what is missing from it."""
        repository_id, registered = await self._registered(guild_id)
        current = await self._github.get_repository(registered.owner, registered.name)
        if current.github_repo_id != registered.github_repo_id:
            raise RepositoryMismatchError(
                f"This server is registered to {registered.full_name}, and GitHub now serves a "
                "different repository under that name. Somebody else has taken it, so nothing "
                "was mirrored."
            )

        # Both lists before either is mirrored, even when the first exhausts the cap: one extra
        # GitHub call buys an honest count of how many are still untracked.
        work: list[TrackedSnapshot] = []
        already = 0
        for object_type in _kinds(scope):
            found = await self._open_items(object_type, current)
            threaded = await self._threaded(repository_id, object_type)
            for item in found:
                if item.github_object_id in threaded:
                    already += 1
                else:
                    work.append(item)

        mirrored, failed = await self._mirror(work[: self._cap])
        return RefreshOutcome(
            full_name=current.full_name,
            mirrored=mirrored,
            already=already,
            failed=failed,
            left=len(work) - mirrored,
        )

    async def _registered(self, guild_id: int) -> tuple[int, RepositorySnapshot]:
        """The repository this server is bound to, as plain values.

        A snapshot rather than a row: everything after this happens outside the session, and a
        detached row is a lazy load waiting to fail.
        """
        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
            if stored is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")
            owner, _, name = stored.repo_name.partition("/")
            return stored.id, RepositorySnapshot(
                github_repo_id=stored.github_repo_id,
                owner=owner,
                name=name,
                html_url=stored.repo_url,
            )

    async def _open_items(
        self, object_type: ObjectType, repository: RepositorySnapshot
    ) -> Sequence[TrackedSnapshot]:
        if object_type is ObjectType.PR:
            return await self._github.list_open_pull_requests(repository)
        return await self._github.list_open_issues(repository)

    async def _threaded(self, repository_id: int, object_type: ObjectType) -> set[int]:
        """The items of one kind that already have a thread, in one query.

        Keyed on the thread, not the row: the row is committed before the Discord call that gives
        it one, so an item whose thread creation was refused is recorded here and invisible in
        the channel, and nothing but a webhook ever comes back for it.
        """
        async with self._sessionmaker() as session:
            state = await TrackedItemStore(session).mirrored_state(
                repository_id=repository_id, object_type=object_type
            )
        return {
            github_object_id
            for github_object_id, (_, thread_id) in state.items()
            if thread_id is not None
        }

    async def _mirror(self, work: Sequence[TrackedSnapshot]) -> tuple[int, int]:
        """One item at a time, counting what landed and what did not.

        Sequential rather than gathered: every sync holds a Postgres advisory lock, and holds the
        connection it took it on for the whole of its Discord phase, so running these at once
        would put the pool's fifteen connections against however many items the cap allows.
        """
        mirrored = 0
        failed = 0
        for snapshot in work:
            try:
                result = await self._syncs[snapshot.object_type].sync(snapshot)
            except ShannonError as refusal:
                # A single thread Discord refuses must not take every item after it down.
                failed += 1
                logger.warning(
                    "could not mirror %s#%s on a refresh: %s",
                    snapshot.repository.full_name,
                    snapshot.number,
                    refusal,
                )
            except Exception:
                # Letting an unexpected failure out would strand the command with no reply.
                failed += 1
                logger.exception(
                    "an unexpected failure mirroring %s#%s on a refresh",
                    snapshot.repository.full_name,
                    snapshot.number,
                )
            else:
                if result.outcome is SyncOutcome.NOT_TRACKED:
                    # A refusal on the repository or the channel, not on this item, so every
                    # item after it would be refused identically.
                    raise SyncFailedError(
                        "The repository is registered but has no channel mapped for that. "
                        "Run /set_channel first."
                    )
                if result.synced:
                    mirrored += 1
        return mirrored, failed


def _kinds(scope: RefreshScope) -> tuple[ObjectType, ...]:
    """Pull requests first, so a capped run spends it on reviews, not the issue backlog."""
    if scope is RefreshScope.PULL_REQUESTS:
        return (ObjectType.PR,)
    if scope is RefreshScope.ISSUES:
        return (ObjectType.ISSUE,)
    return (ObjectType.PR, ObjectType.ISSUE)
