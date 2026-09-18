"""Mirroring the open items that nothing ever opened a thread for.

The bot learns about an item when a webhook arrives for it, and nothing revisits one. So anything
open before the repository was registered, and anything open through a gap in delivery, has no
thread and never will: `/pr` and `/issue` mend one at a time, if somebody notices. Issue #74.

The shape is the board poller's, because it is the same job: read a list from GitHub, compare it
against what is already stored in one query, and mirror the gaps one at a time, tolerating an item
that fails without abandoning the rest. What differs is that this is a person waiting on a reply
rather than a timer, so it is capped and it counts what it did.

Nothing here pings. That is not a rule this module keeps; it is a consequence of the sync services
it is handed being built without a notifier, which is `container._refresh`.
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

# How many items one run will mirror.
#
# The number is fixed by Discord rather than by the deployment, which is why it is here and not in
# Settings. A command has fifteen minutes after it defers, and each item costs two Discord calls:
# the thread and the message in it. Twenty-five of those is fifty calls, which is about two
# minutes even at the pessimistic end, where discord.py is sitting out thread-create rate limits
# rather than failing on them.
#
# A hundred would still fit, and that is the argument against it. It fits with no margin, and the
# failure when the margin goes is the worst one on offer: the work is done, the threads are in the
# channel, and the reply cannot be delivered because the token has expired. What that looks like
# from the outside is nothing happening, so it gets run again.
#
# Twenty-five also happens to be as many new threads as a channel can absorb at once without the
# people reading it losing the thread, and it makes "run it again" a sentence somebody will
# actually act on: a hundred-item backlog is four runs, not twenty.
MIRRORED_PER_RUN = 25


class RefreshScope(StrEnum):
    """Which kinds one run covers."""

    EVERYTHING = "everything"
    PULL_REQUESTS = "pull_requests"
    ISSUES = "issues"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What a run did, in numbers, for the command to turn into a sentence.

    `failed` is inside `left` rather than beside it. An item this run could not mirror is still
    untracked and a later run will try it again, so the number somebody should act on is the one
    that counts it. Reported separately only so the reply can say some of them went wrong.
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

        # Both lists before either is mirrored, even when the first exhausts the cap. It costs one
        # call that might not have been needed and buys the only honest answer to "how many are
        # still untracked", which is the number the reply asks somebody to act on.
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

        Read out into a snapshot rather than carried as a row, because everything after this
        happens outside the session and a detached row is a lazy load waiting to fail.
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

        A thread rather than a row, and the difference is the point. The row is committed before
        the Discord call that gives it one, so an item whose thread creation was refused is
        recorded here and invisible in the channel. Counting it as tracked would leave it that way
        for good, because nothing but a webhook ever comes back for it.
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

        Sequential rather than gathered. Every sync holds a Postgres advisory lock, and holds the
        connection it took it on for the whole of its Discord phase, so running these at once
        would put the pool's fifteen connections against however many items the cap allows.
        """
        mirrored = 0
        failed = 0
        for snapshot in work:
            try:
                result = await self._syncs[snapshot.object_type].sync(snapshot)
            except ShannonError as refusal:
                # One item at a time, or a single thread Discord refuses takes every item after
                # it down with it and the reply says nothing was mirrored when most of it was.
                failed += 1
                logger.warning(
                    "could not mirror %s#%s on a refresh: %s",
                    snapshot.repository.full_name,
                    snapshot.number,
                    refusal,
                )
            except Exception:
                # Caught for the same reason and a stronger one: a surprise on item seven has
                # nothing to do with items eight to twenty-five, and letting it out would strand
                # the command with no reply at all. The traceback goes to the log whole.
                failed += 1
                logger.exception(
                    "an unexpected failure mirroring %s#%s on a refresh",
                    snapshot.repository.full_name,
                    snapshot.number,
                )
            else:
                if result.outcome is SyncOutcome.NOT_TRACKED:
                    # Nothing about this item decided that. The sync refuses on the repository or
                    # on the channel, so every item after it would be refused identically, each
                    # opening a session and writing the same warning. One is enough to learn it.
                    raise SyncFailedError(
                        "The repository is registered but has no channel mapped for that. "
                        "Run /set_channel first."
                    )
                if result.synced:
                    mirrored += 1
        return mirrored, failed


def _kinds(scope: RefreshScope) -> tuple[ObjectType, ...]:
    """Pull requests first, so a run that reaches its cap spends it on the reviews people are
    waiting on rather than on the issue backlog behind them."""
    if scope is RefreshScope.PULL_REQUESTS:
        return (ObjectType.PR,)
    if scope is RefreshScope.ISSUES:
        return (ObjectType.ISSUE,)
    return (ObjectType.PR, ObjectType.ISSUE)
