"""Proving that a Discord account belongs to a particular GitHub one.

Two halves of one round trip. `IdentityVerificationStore` holds the outstanding links and is the
only thread back from an unauthenticated callback to the person who asked for it;
`VerifiedIdentityStore` holds what GitHub said once they followed one.

Only `/unregister` reads any of this, and it is here rather than beside `user_links` because the
two are different kinds of claim. A link is what somebody typed about themselves. This is what
GitHub answered when asked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import interval
from shannon.db.models import IdentityVerification, VerifiedIdentity


class IdentityVerificationStore:
    """The one-time links handed out by `/unregister`, and the single use of each."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self, *, state: str, guild_id: int, discord_user_id: int, lifetime: timedelta
    ) -> None:
        """Hand out a link that expires a fixed time from now.

        A lifetime rather than a moment, and the moment is computed by the database. `consume`
        expires a row against `now()`, so a row stamped by the application would be compared
        against a different clock: the two disagree by however far the process and the database
        have drifted, and a link would then live slightly longer or slightly less long than it
        says. Both sides come from one place instead.
        """
        await self._session.execute(
            pg_insert(IdentityVerification).values(
                state=state,
                discord_guild_id=guild_id,
                discord_user_id=discord_user_id,
                expires_at=func.now() + interval(lifetime),
            )
        )

    async def consume(self, state: str) -> tuple[int, int] | None:
        """Spend a link, answering whose it was, or None if it cannot be spent.

        One statement, and that is the whole design. The filter, the stamp and the answer happen
        together, so two clicks on the same link race in Postgres and exactly one of them wins.
        Reading the row and then updating it would leave a window between the two in which both
        clicks see an unconsumed row, and the losing one would unbind a repository on the strength
        of a link that had already been used.

        One answer for expired, already used and never existed. They are the same thing to whoever
        is looking at the page, and telling them apart out loud would confirm to somebody guessing
        states that a particular one was real.

        `func.now()` rather than a clock passed in, because both sides of the comparison have to
        come from the same place. A row stamped by the application and expired against the
        database would drift apart by however far the two clocks disagree.
        """
        spent = await self._session.execute(
            update(IdentityVerification)
            .where(
                IdentityVerification.state == state,
                IdentityVerification.consumed_at.is_(None),
                IdentityVerification.expires_at > func.now(),
            )
            .values(consumed_at=func.now())
            .returning(IdentityVerification.discord_guild_id, IdentityVerification.discord_user_id)
        )
        found = spent.first()
        return (found[0], found[1]) if found is not None else None

    async def prune(self, *, keep_for: timedelta) -> int:
        """Drop links that are long past being usable.

        Spent and expired alike: neither can be redeemed again, and a state nobody can use is a
        state worth not keeping. Whatever is still live is left alone however old the row is,
        which is the same rule the delivery queue's own pruning follows.
        """
        result = await self._session.execute(
            delete(IdentityVerification).where(
                IdentityVerification.expires_at < func.now() - keep_for
            )
        )
        return result.rowcount or 0


class VerifiedIdentityStore:
    """Who a Discord account proved to be on GitHub, and how long ago."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def remember(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        github_login: str,
        github_user_id: int,
        verified_at: datetime,
    ) -> None:
        """Record a proof, replacing any earlier one for that person in that server.

        Replaced rather than appended, because only the most recent one can permit anything and a
        history of who somebody used to be is not this table's business. Somebody legitimately
        moving between GitHub accounts overwrites cleanly.
        """
        login = github_login.strip()
        await self._session.execute(
            pg_insert(VerifiedIdentity)
            .values(
                discord_guild_id=guild_id,
                discord_user_id=discord_user_id,
                github_login=login,
                github_user_id=github_user_id,
                verified_at=verified_at,
            )
            .on_conflict_do_update(
                constraint="uq_verified_identities_guild_discord",
                set_={
                    "github_login": login,
                    "github_user_id": github_user_id,
                    "verified_at": verified_at,
                },
            )
        )

    async def fresh(
        self, *, guild_id: int, discord_user_id: int, newer_than: datetime
    ) -> str | None:
        """The login this person proved recently, or None if they have not proved one lately.

        The cutoff is the caller's, because how recent is recent enough is a policy question and
        this is a table. Filtered here rather than returned for the caller to check, because a
        method handing back a stale row relies on every caller remembering to look at the date,
        and the one that forgets is the one that unbinds a repository on a year-old proof.

        The login alone rather than the row. It is the whole of what the permission check needs,
        and anything else handed out is something a caller could decide on instead.
        """
        return await self._session.scalar(
            select(VerifiedIdentity.github_login).where(
                VerifiedIdentity.discord_guild_id == guild_id,
                VerifiedIdentity.discord_user_id == discord_user_id,
                VerifiedIdentity.verified_at >= newer_than,
            )
        )
