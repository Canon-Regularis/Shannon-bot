from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.assignments import ItemAssignmentStore
from shannon.db.stores.user_links import UserLinkStore
from shannon.discord_bot.threads import PostsToThread
from shannon.domain.enums import ActorRole

logger = logging.getLogger(__name__)

Renderer = Callable[[Sequence[str], Mapping[str, int]], str]


class ResolvesMentions(Protocol):
    """Turning the names on an item into something Discord will notify.

    Injected rather than imported, because there are two of these and neither is more the real
    one: a login resolves to an account and a team slug resolves to a role. What differs is the
    table read and the syntax the renderer writes, and neither is this class's business.
    """

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> Mapping[str, int]: ...


Mentions = Callable[[AsyncSession], ResolvesMentions]


class ActorNotifier:
    """Pings the people in one role once each.

    `item_assignments.notified_at` is the record of who has already been told. Someone removed
    and then added back gets a fresh row, and so is pinged again, which is what re-requesting a
    review or reassigning an issue is asking for.

    The role and the wording are injected, because a reviewer being asked to review and an
    assignee being handed a ticket are the same mechanism with different words.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        threads: PostsToThread,
        *,
        role: ActorRole,
        render: Renderer,
        mentions: Mentions = UserLinkStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._threads = threads
        self._role = role
        self._render = render
        self._mentions = mentions

    async def notify(
        self, *, tracked_item_id: int, thread_id: int, guild_id: int
    ) -> tuple[str, ...]:
        """Ping whoever has not been pinged yet, returning the logins that were.

        The pings are claimed before the message goes out, not after. Two syncs of one item
        overlap whenever somebody runs /pr while an event for it is in flight, and a delivery
        that fails partway through is retried from the top; either would send the same person
        the same ping twice if the claim came last.
        """
        # Deliberately not shielded. Shielding this looks like it protects the claim, and does
        # the opposite: the await raises at once while the claim carries on and commits, so
        # `logins` is never bound, the guard below is never entered, and the ping is owed to
        # nobody for ever. Unshielded, a cancellation here aborts the transaction before it
        # commits and nothing was claimed, which is the outcome worth having. Once this returns
        # there is no await before the guard, so nothing can land in between.
        claimed, mentions = await self._claim(tracked_item_id, guild_id)
        if not claimed:
            return ()
        logins = tuple(sorted(claimed))

        try:
            await self._threads.post(thread_id=thread_id, content=self._render(logins, mentions))
        except BaseException:
            # Nothing was said, so the ping is owed again; late beats twice or never.
            #
            # BaseException because cancellation counts as a failure here: the worker puts a
            # deadline on each delivery and cancels the handler where it stands, and discord.py
            # sleeps through a rate limit rather than failing, so where it stands is often
            # exactly here. Shielded so that cancellation cannot interrupt the hand-back itself.
            # Under cancellation the await returns at once and the release lands a moment later,
            # well before the retry five seconds on.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._release(tracked_item_id, claimed))
            raise

        logger.info("pinged %s %s on tracked item %s", self._role, logins, tracked_item_id)
        return logins

    async def _claim(
        self, tracked_item_id: int, guild_id: int
    ) -> tuple[Mapping[str, int | None], Mapping[str, int]]:
        """Take the pings nobody has sent yet, and work out how to address them.

        The claim answers with the account beside each name, because this is the one mention
        built from a stored name rather than from the payload in hand. A login is not an
        identity, and the ping is the mention that actually notifies somebody.
        """
        async with self._sessionmaker() as session, session.begin():
            claimed = await ItemAssignmentStore(session).claim_notifications(
                tracked_item_id, self._role
            )
            if not claimed:
                return (), {}
            mentions = await self._mentions(session).resolve_many(guild_id=guild_id, people=claimed)
        # The account beside each name goes back to the caller as well, because the hand-back
        # below has to find these rows again after a gap long enough for a rename to land in.
        return claimed, mentions

    async def _release(self, tracked_item_id: int, claimed: Mapping[str, int | None]) -> None:
        async with self._sessionmaker() as session, session.begin():
            await ItemAssignmentStore(session).release_notifications(
                tracked_item_id, self._role, claimed
            )
