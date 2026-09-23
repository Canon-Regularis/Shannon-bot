"""Finding the thread one GitHub item is mirrored into.

Four things ask this: the note mirror, the check announcer, the approval round-up, and whatever
asks next. Each one had its own copy of the same two reads and the same two log lines, differing
only in the noun they used for what had arrived, which is how a fourth copy gets written rather
than a third reused.

What is NOT here is anything a caller does with the answer. The note mirror goes on to resolve
the mentions in a body and the others do not; that part stays where it is, because sharing it
would mean a parameter for each caller's half of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import ItemNotReadyError
from shannon.domain.models import RepositorySnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ItemInThread:
    """A tracked item with somewhere to post about it, and the server that somewhere is in."""

    tracked_item_id: int
    thread_id: int
    guild_id: int


async def in_its_thread(
    session: AsyncSession,
    *,
    repository: RepositorySnapshot,
    number: int,
    object_type: ObjectType,
    about: str,
) -> ItemInThread | None:
    """Where to post about this item, or None where there is nowhere and never will be.

    Two answers and a third that is neither. A repository no guild registered and an item this
    server does not track are both final: nothing later makes them true, so the delivery is
    finished rather than retried, and the log line is the only place anybody sees that a
    registered repository is sending events about items nobody is watching.

    An item that IS tracked and has no thread yet is the third, and it raises rather than
    answering None. A check suite can finish while the `opened` delivery that builds the thread is
    still behind a Discord outage, and nothing revisits a delivery that reported nothing to do —
    so the only way that item is ever mirrored is if this one comes round again.

    By number rather than by id, which is not a convenience: a pull request reports its ISSUE id
    in comment payloads, and that never matches the pull request id stored against the item.

    `about` names what arrived, for the log alone. Takes a session rather than a sessionmaker, so
    a caller already inside one is not made to open a second; everything here is a read.
    """
    found = await RepositoryStore(session).get_by_github_id(repository.github_repo_id)
    if found is None:
        logger.info(
            "%s arrived for %s, which is not registered to any guild", about, repository.full_name
        )
        return None

    item = await TrackedItemStore(session).get_by_number(
        repository_id=found.id, number=number, object_type=object_type
    )
    if item is None:
        logger.info(
            "%s on %s#%s is not tracked here, ignoring", about, repository.full_name, number
        )
        return None

    if item.discord_thread_id is None:
        raise ItemNotReadyError(f"{repository.full_name}#{number} has no thread yet")

    return ItemInThread(
        tracked_item_id=item.id,
        thread_id=item.discord_thread_id,
        guild_id=found.discord_guild_id,
    )
