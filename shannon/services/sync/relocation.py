"""Moving the threads a changed channel mapping left behind.

A repository registered in the wrong channel puts every thread there. Correcting the mapping fixes
where NEW threads go and leaves every existing one exactly where it was, being edited in the wrong
place for ever, because the sync reuses whatever thread the row points at without ever asking where
that is. Issue #78.

Discord cannot move a thread between channels, so a move here means opening a replacement in the
right one, leaving a line in the old thread saying where its item went, and shutting it.

The shape is `RepositoryRefresh`'s, for the same reasons: a person is waiting on a reply, so it is
capped and it counts what it did, and one item failing must not take the rest with it.
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

# Two answers about where a thread is that are not a channel id. Sentinels rather than None,
# because None is what Discord says about a thread that has gone, and that is a different answer
# from a lookup nobody was allowed to make.
_GONE = object()
_UNKNOWN = object()

# How many threads one run will move.
#
# Ten rather than the refresh's twenty-five, and it is the same budget rather than a smaller one.
# A refresh costs two Discord calls an item; a move costs four typically and seven at worst: the
# replacement, the signpost, the shut, and sometimes an unarchive, a lookup, and a shut on the new
# thread for an item that was already finished.
#
# Leaning on the fifteen minutes a command gets would be wrong here for a reason that does not
# apply to a refresh. Creating threads is what Discord rate-limits hardest, and discord.py sleeps
# through a 429 rather than raising, so the one thing that would spend the budget gives no warning
# before it does. And the failure at the edge is worse: the replacements exist and the old threads
# are locked, but the token has expired and the reply never lands, so from the outside nothing
# happened and somebody runs it again.
MOVED_PER_RUN = 10

# How many threads one run will ask Discord about. Only the rows claimed before the channel column
# existed need asking, and the answer is written down, so this converges to nothing after a run or
# two. Its own budget because a lookup is cheap and a move is not, and a run that spends all its
# lookups has still recorded every answer it got.
ASKED_PER_RUN = MOVED_PER_RUN * 4


@dataclass(frozen=True, slots=True)
class Mirror:
    """One kind's way back into Discord: read it from GitHub, then sync it.

    A ticket has neither. A draft board card exists nowhere but the board and has no endpoint to
    fetch it by number, which is why the kinds are a mapping and not a pair of arguments: a kind
    with no mirror takes the other route, and that is readable rather than special-cased.
    """

    service: SyncsItems
    fetch: Fetcher


@dataclass(frozen=True, slots=True)
class RelocationOutcome:
    """What a run did, for the command to turn into a sentence.

    `failed` is inside `left`, as in `RefreshOutcome`: an item that could not be moved is still in
    the wrong channel and a later run will try it again.

    `left` is an upper bound rather than a count, and that is worth knowing. A row that remembers
    no channel cannot be told from one already in the right place without asking Discord, so
    everything past the budgets is assumed still stranded. A second run answers exactly.
    """

    moved: int
    failed: int
    left: int


class MovesThreadsBetweenChannels(FindsThreads, PostsToThread, ShutsThread, Protocol):
    """What this path needs of Discord, and deliberately no more.

    It finds out where a thread is, says one line in the one it is leaving, and shuts that one.
    It cannot open a thread or delete one: the replacement is opened by the sync service, through
    the binding that already knows how to attach it to the row without leaving an orphan.
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
                # done. Counted as a failure because it is one, and it stays a candidate.
                failed += 1
                continue
            if where is _GONE or where == channel_id:
                # Nothing to do and nothing wrong: either the pointer has been let go of, or the
                # thread was in the right channel all along and the row simply did not say so.
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
                # A surprise on one item has nothing to do with the rest, and letting it out would
                # strand the command with no reply at all. The traceback goes to the log whole.
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
        moves where issue threads go too, on a server that never mapped issues. Relocating only
        the named kind would reproduce this very bug for the other one, triggered by fixing it.

        A kind with a mapping row of its own is not borrowing and is left alone.
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

        The row first, because it is free and right whenever it has an answer. Discord only for
        the rows written before that column existed, and the answer is written back so a second
        run asks nothing.
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
                # Discord has no such thread. The pointer is worthless, so it is let go of and the
                # item gets a fresh one in the right channel from whatever visits it next.
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

        The signpost goes in before the shut, never after: posting to an archived thread
        unarchives it, so shutting first would be undone by the line itself.

        Both of those are best effort. By the time they run the row already points at the
        replacement, so a refusal costs the signpost and nothing else, and this item is not a
        candidate again.
        """
        mirror = self._mirrors.get(candidate.object_type)
        if mirror is None:
            await self._release_for_the_poller(candidate, channel_id)
            return

        snapshot = await mirror.fetch(owner, name, candidate.number)
        result = await mirror.service.sync(snapshot)
        if result.displaced is None:
            # Nothing moved. Either something attached a thread in the right channel while this
            # was in flight, or the sync refused the item for a reason of its own; either way
            # there is no old thread to say anything in.
            return

        # A displaced thread means the sync reached the branch that writes one, and that
        # branch takes both from the same handle. Asserted rather than branched on, which
        # would add an arm nothing can reach.
        assert result.thread_id is not None
        await self._say_where_it_went(result.displaced, format_thread_moved(result.thread_id))

    async def _release_for_the_poller(self, candidate: StrandedThread, channel_id: int) -> None:
        """A board card, which has no GitHub endpoint to rebuild it from.

        So the order inverts: let go of the pointer first and let the poller open the replacement
        on its next pass, which it will, because a card with no thread is exactly the state it
        already repairs. The signpost names the channel rather than a thread, there being none
        yet to name.
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
            # One arm for all of them. The item has already moved, so what is lost is the line and
            # the lock on a thread nothing will write to again.
            logger.warning(
                "moved the item off thread %s but could not say so in it: %s", thread_id, refusal
            )
