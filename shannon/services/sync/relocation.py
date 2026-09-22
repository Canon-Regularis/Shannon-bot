"""Moving the threads a changed channel mapping left behind.

Correcting a mapping only redirects new threads: the sync reuses the thread a row points at (#78).
Discord cannot move a thread between channels: a move is a new thread, a line in the old, a shut.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.channel_mappings import ChannelMappingStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.thread_pointers import ThreadPointerStore
from shannon.db.stores.tracked_items import StrandedThread, TrackedItemStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import format_thread_moved, format_thread_moving
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import FindsThreads, PostsToThread, ShutsThread
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError, ShannonError
from shannon.domain.models import Fetcher
from shannon.services.sync.items import SyncsItems
from shannon.services.sync.policies import channel_fallbacks

logger = logging.getLogger(__name__)

# Two answers that are not a channel id. Sentinels rather than None, because None is what Discord
# says about a thread that has gone, and that is a different answer from a lookup that was refused.
_GONE = object()
_UNKNOWN = object()

# A move costs four Discord calls typically and seven at worst, against a refresh's two an item.
# Creating threads is what Discord rate-limits hardest, and discord.py sleeps through a 429 rather
# than raising, so nothing warns before the interaction token expires and the reply never lands.
MOVED_PER_RUN = 10

# Only rows claimed before the channel column existed need asking, and the answer is written back,
# so this converges to nothing after a run or two. Its own budget because a lookup is cheap and a
# move is not, and a run that spends all its lookups has still recorded every answer it got.
ASKED_PER_RUN = MOVED_PER_RUN * 4


@dataclass(frozen=True, slots=True)
class Mirror:
    """One kind's way back into Discord: read it from GitHub, then sync it.

    A draft board card has no endpoint to fetch it by number, so some kinds have no mirror.
    """

    service: SyncsItems
    fetch: Fetcher


@dataclass(frozen=True, slots=True)
class RelocationOutcome:
    """What a run did, for the command to turn into a sentence.

    `failed` is counted inside `left`, and `left` is an upper bound: past the budgets a row that
    remembers no channel is assumed stranded rather than asked about.
    """

    moved: int
    failed: int
    left: int


class MovesThreadsBetweenChannels(FindsThreads, PostsToThread, ShutsThread, Protocol):
    """What this path needs of Discord.

    Opening the replacement is elsewhere: the sync service attaches the new thread to the row.
    """


class ThreadRelocation:
    """Give the threads a remap left behind ones in the channel it now names."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: MovesThreadsBetweenChannels,
        *,
        mirrors: Mapping[ObjectType, Mirror],
        cap: int = MOVED_PER_RUN,
        asked: int = ASKED_PER_RUN,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._mirrors = dict(mirrors)
        self._cap = cap
        self._asked = asked

    async def relocate(
        self, *, guild_id: int, object_type: ObjectType, channel_id: int
    ) -> RelocationOutcome:
        """Move what this mapping change left in the wrong place."""
        repository_id, owner, name = await self._registered(guild_id)
        kinds = await self._kinds_now_pointing_here(repository_id, object_type)

        async with self._sessionmaker() as session:
            candidates = await TrackedItemStore(session).stranded_threads(
                repository_id=repository_id, kinds=kinds, channel_id=channel_id
            )

        moved = 0
        failed = 0
        settled = 0
        asked = 0

        for candidate in candidates:
            if moved >= self._cap or asked >= self._asked:
                break

            where, spent = await self._where_it_is(candidate)
            asked += spent
            if where is _UNKNOWN:
                # The lookup was refused rather than answered, so nothing is known and nothing is
                # done. Counted as a failure, and the item stays a candidate.
                failed += 1
                continue
            if where is _GONE or where == channel_id:
                # Nothing wrong: the pointer has been let go of, or the thread was in the right
                # channel all along and the row simply did not say so.
                settled += 1
                continue

            try:
                await self._move(candidate, owner, name, channel_id)
            except ShannonError as refusal:
                failed += 1
                logger.warning(
                    "could not move the thread for tracked item %s: %s",
                    candidate.tracked_item_id,
                    refusal,
                )
            except Exception:
                # One item's surprise must not strand the command with no reply at all. The
                # traceback goes to the log whole.
                failed += 1
                logger.exception(
                    "an unexpected failure moving the thread for tracked item %s",
                    candidate.tracked_item_id,
                )
            else:
                moved += 1

        return RelocationOutcome(moved=moved, failed=failed, left=len(candidates) - moved - settled)

    async def _registered(self, guild_id: int) -> tuple[int, str, str]:
        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
            if stored is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")
            owner, _, name = stored.repo_name.partition("/")
            return stored.id, owner, name

    async def _kinds_now_pointing_here(
        self, repository_id: int, object_type: ObjectType
    ) -> Sequence[ObjectType]:
        """Every kind whose threads this mapping decides, not just the one named.

        Issues fall back to the pull request channel, so pointing pull requests somewhere new
        moves where issue threads go too, on a server that never mapped issues.
        """
        kinds = [object_type]
        async with self._sessionmaker() as session:
            mappings = ChannelMappingStore(session)
            for borrower, lends in channel_fallbacks().items():
                if lends is object_type and await mappings.get(repository_id, borrower) is None:
                    kinds.append(borrower)
        return kinds

    async def _where_it_is(self, candidate: StrandedThread) -> tuple[object, int]:
        """Where this item's thread actually is, and what asking cost.

        Discord is asked only for the rows written before that column existed, and the answer is
        written back, so a second run asks nothing.
        """
        if candidate.channel_id is not None:
            return candidate.channel_id, 0

        try:
            found = await self._threads.channel_of(thread_id=candidate.thread_id)
        except DiscordGatewayError as refusal:
            logger.warning(
                "could not find out where the thread for tracked item %s is: %s",
                candidate.tracked_item_id,
                refusal,
            )
            return _UNKNOWN, 1

        async with self._sessionmaker() as session, session.begin():
            pointers = ThreadPointerStore(session)
            if found is None:
                # Discord has no such thread, so the pointer goes and the item gets a fresh one in
                # the right channel from whatever visits it next.
                await pointers.forget_thread(
                    candidate.tracked_item_id, dead_thread_id=candidate.thread_id
                )
                return _GONE, 1
            await pointers.remember_channel(
                candidate.tracked_item_id,
                thread_id=candidate.thread_id,
                channel_id=found,
            )
        return found, 1

    async def _move(
        self, candidate: StrandedThread, owner: str, name: str, channel_id: int
    ) -> None:
        """Give one item a thread in the right channel, and sign-post the one it left.

        Posting to an archived thread unarchives it, so the signpost goes in before the shut.
        """
        mirror = self._mirrors.get(candidate.object_type)
        if mirror is None:
            await self._release_for_the_poller(candidate, channel_id)
            return

        snapshot = await mirror.fetch(owner, name, candidate.number)
        result = await mirror.service.sync(snapshot)
        if result.displaced is None:
            # Either something attached a thread in the right channel while this was in flight, or
            # the sync refused the item; either way there is no old thread to say anything in.
            return

        # The sync writes both from the same handle, so a displaced thread means there is a new
        # one. Asserted rather than branched on, which would add an arm nothing can reach.
        assert result.thread_id is not None
        await self._say_where_it_went(result.displaced, format_thread_moved(result.thread_id))

    async def _release_for_the_poller(self, candidate: StrandedThread, channel_id: int) -> None:
        """A board card, which has no GitHub endpoint to rebuild it from.

        So the order inverts: the pointer goes and the poller opens the replacement on its next
        pass, as it does for any card with no thread. The signpost names a channel, not a thread.
        """
        async with self._sessionmaker() as session, session.begin():
            await ThreadPointerStore(session).forget_thread(
                candidate.tracked_item_id, dead_thread_id=candidate.thread_id
            )
        await self._say_where_it_went(candidate.thread_id, format_thread_moving(channel_id))

    async def _say_where_it_went(self, thread_id: int, line: str) -> None:
        try:
            await self._threads.post(thread_id=thread_id, panel=Panel.of_text(line))
            await self._threads.set_shut(thread_id=thread_id, shut=True)
        except DiscordGatewayError as refusal:
            # The item has already moved, so a refusal costs the line and the lock on a thread
            # nothing will write to again.
            logger.warning(
                "moved the item off thread %s but could not say so in it: %s", thread_id, refusal
            )
