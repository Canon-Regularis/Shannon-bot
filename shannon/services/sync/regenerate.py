"""Redrawing one item's block from GitHub, for a thread nothing else will ever come back to.

Issue #65. Every other thing that rewrites a block is something arriving: a delivery, a manual
`/pr`, a `/set_*`, the board poller. An item that has gone quiet gets none of them, and a closed
one never will again, so whatever its block said on the day it was last touched is what it says
for ever. That is wrong for book-keeping, and it is also why somebody who ran `/link` after their
thread was opened by `/refresh` stays named in plain text: the mapping is read fresh on every
sync, but no sync ever runs again.

The fourth service of this shape, beside `ManualSync` (by link), `RepositoryRefresh` (a whole
backlog) and `ThreadRelocation` (threads in the wrong channel). What separates it from `/pr` is
not its logic, which is nearly the same, but what its sync services are built WITH: no notifier,
and no allow-list. `/pr` carries notifiers, so redrawing a pull request that closed last month
would post fresh ping lines to everybody still holding an unspent claim on it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.domain.enums import ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.services.sync.items import SyncOutcome
from shannon.services.sync.manual import SyncFailedError
from shannon.services.workflow import FoundItem, ItemKind, WorkflowRefusedError, locate

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegenerateOutcome:
    """What the redraw did, for a reply that has to say more than "done".

    `created` and `shut_refused` are both surprises rather than details. The first means the
    thread somebody ran this in is not the thread they are now looking at, and the second means a
    finished item's thread has been left open, which is worse than it was before.
    """

    full_name: str
    number: int
    thread_id: int
    created: bool
    shut_refused: bool


class ItemRegeneration:
    """Reads one item from GitHub again and rewrites the block in its thread."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], kinds: Mapping[ObjectType, ItemKind]
    ) -> None:
        self._sessionmaker = sessionmaker
        self._kinds = kinds

    async def regenerate(self, *, thread_id: int) -> RegenerateOutcome:
        """Redraw the item whose thread this is.

        Everything after the fetch is the ordinary sync path and needs nothing of its own. A
        command passes no delivery number, so the staleness guard lets it through where it would
        refuse a late webhook; an archived thread is woken before the write and shut again after
        it; and the block is EDITED, which notifies nobody whatever it names.

        Two things it does that `/pr` does not, both worth knowing about rather than hiding:

        A thread somebody locked by hand on an item that is still open gets unlocked, because the
        sync puts the lock back the way the item says it should be. `/pr` does the same, and it is
        the behaviour that gives a reopened issue its thread back.

        The people on the item are re-read, which can clear a spent review-request claim. Nothing
        is posted here, so what that costs is a ping on the item's NEXT genuine delivery - which,
        for the closed items this exists for, never comes. Guarding it would mean refusing to
        correct a stale assignee list, which is the feature.
        """
        found = await locate(self._sessionmaker, thread_id)
        kind = self._kinds.get(found.object_type)
        if kind is None:
            # A project board card. There is no page on GitHub to read it back from, and the
            # poller already opens a replacement for any card whose thread has gone.
            raise WorkflowRefusedError(
                f"{found.full_name} is a project board card, so there is nothing on GitHub to "
                "read it back from. The board updates its own threads."
            )

        snapshot = await kind.fetch(found.owner, found.name, found.number)
        _refuse_a_different_repository(found, snapshot.repository.github_repo_id)

        result = await kind.sync.sync(snapshot)
        if result.outcome is SyncOutcome.NOT_TRACKED:
            raise SyncFailedError(
                f"{found.full_name} has no channel mapped for that kind of item. "
                "Run /set_channel first."
            )
        if not result.synced or result.thread_id is None:
            # A redraw that writes nothing and reports success is the worst answer available
            # here: the whole reason somebody ran it is that the block is wrong. `ManualSync` has
            # this hole and it does not matter there, because `/pr` is asked to mirror an item
            # rather than to correct one.
            raise SyncFailedError(
                f"{found.full_name}#{found.number} could not be redrawn just now. "
                "Something else is mid-change on it; try again in a moment."
            )

        logger.info("redrew %s#%s from GitHub", found.full_name, found.number)
        return RegenerateOutcome(
            # Off the snapshot rather than off the row. The sync has just followed any rename, so
            # the snapshot is the current name and the row the command started from is not.
            full_name=snapshot.repository.full_name,
            number=snapshot.number,
            thread_id=result.thread_id,
            created=result.created,
            shut_refused=result.shut_refused,
        )


def _refuse_a_different_repository(found: FoundItem, fetched: int) -> None:
    """Refuse an answer that came from somebody else's repository.

    The fetch addresses GitHub by the stored `owner/name`, and a name is not an identity: GitHub
    frees one the moment a repository is renamed, transferred or deleted, and nothing corrects the
    stored one until an item webhook arrives, which for a repository renamed away never happens.

    Unchecked, the sync resolves the fetched snapshot by ITS own repository id and rewrites a
    thread in whichever server registered that name now. The same check guards `/pr` and the
    workflow commands, for the same reason.
    """
    if fetched != found.github_repo_id:
        raise RepositoryMismatchError(
            f"{found.full_name} is not the repository this server registered any more. "
            "It has been renamed or replaced on GitHub, and somebody else holds that name now."
        )
