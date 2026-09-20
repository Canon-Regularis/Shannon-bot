"""Proving that a Discord account belongs to a particular GitHub one.

Two halves of one round trip: `IdentityVerificationStore` holds the outstanding links and is the
only thread back from an unauthenticated callback to the person who asked for it,
`VerifiedIdentityStore` holds what GitHub said once they followed one. Only `/unregister` reads
any of it. Separate from `user_links`, which records what somebody typed about themselves.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import interval, rows_changed
from shannon.db.models import IdentityVerification, VerifiedIdentity


class IdentityVerificationStore:
    """The one-time links handed out by `/unregister`, and the single use of each."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self, *, state: str, guild_id: int, discord_user_id: int, lifetime: timedelta
    ) -> None:
        """Hand out a link that expires a fixed time from now.

        The moment is computed by the database, because `consume` expires a row against `now()`
        and a row stamped by the application would be compared against a different clock.
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

        Filter, stamp and answer in one statement, so two clicks on the same link race in
        Postgres and exactly one of them wins. One answer for expired, already used and never
        existed: telling them apart would confirm to somebody guessing states that a particular
        one was real.
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
        """Drop links that are long past being usable."""
        changed = await rows_changed(
            self._session,
            delete(IdentityVerification).where(
                IdentityVerification.expires_at < func.now() - keep_for
            ),
        )
        return changed or 0


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
        """Record a proof, replacing any earlier one for that person in that server."""
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

        How recent counts as recent is the caller's policy. Filtering here rather than handing
        back a stale row stops a caller unbinding a repository on a year-old proof.
        """
        found: str | None = await self._session.scalar(
            select(VerifiedIdentity.github_login).where(
                VerifiedIdentity.discord_guild_id == guild_id,
                VerifiedIdentity.discord_user_id == discord_user_id,
                VerifiedIdentity.verified_at >= newer_than,
            )
        )
        return found
