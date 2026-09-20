"""Redrawing one item's block from GitHub, for a thread nothing else will ever come back to.

A closed item gets no further deliveries, so its block says whatever it said on the day it was
last touched. The sync services here are built with no notifier and no allow-list: `/pr` carries
notifiers, so redrawing an item that closed last month would post fresh ping lines to everybody
still holding an unspent claim on it.
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

    `created` means the thread somebody ran this in is not the thread they are now looking at;
    `shut_refused` means a finished item's thread has been left open.
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

        A thread locked by hand on an item that is still open gets unlocked, because the sync puts
        the lock back the way the item says it should be. Re-reading the people on the item can
        clear a spent review-request claim, which costs a ping on its next genuine delivery.
        """
        found = await locate(self._sessionmaker, thread_id)
        kind = self._kinds.get(found.object_type)
        if kind is None:
            # A project board card: nothing on GitHub to read it back from, and the poller
            # already opens a replacement for any card whose thread has gone.
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
            # A redraw that writes nothing and reports success is the wrong answer: the
            # reason somebody ran it is that the block is wrong.
            raise SyncFailedError(
                f"{found.full_name}#{found.number} could not be redrawn just now. "
                "Something else is mid-change on it; try again in a moment."
            )

        logger.info("redrew %s#%s from GitHub", found.full_name, found.number)
        return RegenerateOutcome(
            # Off the snapshot rather than the row: the sync has just followed any rename,
            # so the row the command started from holds the old name.
            full_name=snapshot.repository.full_name,
            number=snapshot.number,
            thread_id=result.thread_id,
            created=result.created,
            shut_refused=result.shut_refused,
        )


def _refuse_a_different_repository(found: FoundItem, fetched: int) -> None:
    """Refuse an answer that came from somebody else's repository.

    The fetch addresses GitHub by the stored `owner/name`, and GitHub frees a name the moment a
    repository is renamed, transferred or deleted; for one renamed away, no webhook ever arrives
    to correct the stored name. Unchecked, the sync resolves the fetched snapshot by its own
    repository id and rewrites a thread in whichever server registered that name now.
    """
    if fetched != found.github_repo_id:
        raise RepositoryMismatchError(
            f"{found.full_name} is not the repository this server registered any more. "
            "It has been renamed or replaced on GitHub, and somebody else holds that name now."
        )
