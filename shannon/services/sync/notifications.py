from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import Notify, PostsToThread
from shannon.domain.enums import ActorRole

logger = logging.getLogger(__name__)

Renderer = Callable[[Sequence[str], Mapping[str, int]], Panel]


class ResolvesMentions(Protocol):
    """Turns the names on an item into something Discord will notify.

    Injected because there are two: a login resolves to an account, a team slug to a role.
    """

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> Mapping[str, int]: ...


Mentions = Callable[[AsyncSession], ResolvesMentions]


class FindsMutedMembers(Protocol):
    """Which of a set of Discord accounts this bot is still allowed to notify.

    Injected like `ResolvesMentions`, and off unless the wiring turns it on.
    """

    async def may_be_pinged(self, *, guild_id: int, ids: Iterable[int]) -> tuple[int, ...]: ...


Muted = Callable[[AsyncSession], FindsMutedMembers]


class ActorNotifier:
    """Pings the people in one role once each.

    `notified_at` on `item_assignments` records who was told; re-adding someone makes a fresh row.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        threads: PostsToThread,
        *,
        role: ActorRole,
        render: Renderer,
        mentions: Mentions = UserLinkStore,
        muted: Muted | None = None,
        the_block_pings_them: bool = True,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._role = role
        self._render = render
        self._mentions = mentions
        # Which of the people about to be named may be notified, or None where the ids are not
        # people at all: the team notifier's ids are role ids, which never sit in `muted_members`.
        self._muted = muted
        # Whether the metadata block reaches these people on its own. False for reviewer teams:
        # the block names a team in plain text and never looks one up, so it reaches nobody.
        self._the_block_pings_them = the_block_pings_them

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int, the_block_pinged: bool
    ) -> tuple[str, ...]:
        """Ping whoever has not been pinged yet, returning the logins that were.

        The claim lands before the message: a /pr overlapping an event, or a retried delivery,
        would ping twice. `the_block_pinged` means the block already carried their mentions.
        """
        # Deliberately not shielded: shielding lets the claim commit while the await raises, so
        # `logins` is never bound and the ping is owed to nobody for ever. Unshielded, a
        # cancellation aborts the transaction before it commits and nothing was claimed.
        claimed, mentions, notify = await self._claim(tracked_item_id, guild_id)
        if not claimed:
            return ()
        logins = tuple(sorted(claimed))

        if the_block_pinged and self._the_block_pings_them:
            # Claimed and then thrown away on purpose. The block just posted carries a live
            # mention for each of these people, and spending the claim is what stops the
            # `labeled` delivery arriving in the same second from posting the line anyway.
            logger.info(
                "the block named %s %s on tracked item %s, so nothing is said beside it",
                self._role,
                logins,
                tracked_item_id,
            )
            return ()

        try:
            # Posted even where nobody on it may be notified: the line is the only record that
            # somebody was put on an item after its thread existed.
            await self._threads.post(
                thread_id=thread_id,
                panel=self._render(logins, mentions),
                notify=notify,
            )
        except BaseException:
            # Nothing was said, so the ping is owed again. BaseException because the worker
            # cancels a delivery on its deadline and discord.py sleeps through rate limits rather
            # than failing, so cancellation often lands here; shielded so the hand-back survives.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._release(tracked_item_id, claimed))
            raise

        logger.info("pinged %s %s on tracked item %s", self._role, logins, tracked_item_id)
        return logins

    async def _claim(
        self, tracked_item_id: int, guild_id: int
    ) -> tuple[Mapping[str, int | None], Mapping[str, int], Notify]:
        async with self._sessionmaker() as session, session.begin():
            claimed = await ItemAssignmentStore(session).claim_notifications(
                tracked_item_id, self._role
            )
            if not claimed:
                return {}, {}, None
            mentions = await self._mentions(session).resolve_many(guild_id=guild_id, people=claimed)
            # None where nothing was injected, which leaves the client's own rule in force.
            notify = (
                None
                if self._muted is None
                else await self._muted(session).may_be_pinged(
                    guild_id=guild_id, ids=mentions.values()
                )
            )
        # The accounts go back to the caller too: the hand-back has to find these rows again
        # after a gap long enough for a rename to land in.
        return claimed, mentions, notify

    async def _release(self, tracked_item_id: int, claimed: Mapping[str, int | None]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).release_notifications(
                tracked_item_id, self._role, claimed
            )
