"""Proving that a Discord account belongs to a particular GitHub one.

Two halves of one round trip: `IdentityVerificationStore` holds the outstanding links and is the
only thread back from an unauthenticated callback to the person who asked for it,
`VerifiedIdentityStore` holds what GitHub said once they followed one. Separate from
`user_links`, which records what somebody typed about themselves: one is a claim and the other is
something GitHub vouched for, and telling them apart is the whole point of keeping both.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ProvedAccount:
    """The GitHub account somebody proved they hold, and when they proved it.

    The id as well as the login, because the id is the part that lasts: GitHub reassigns a freed
    name, so a proof answered by name alone cannot be held against a stored link without asking
    which account that name means today.
    """

    login: str
    github_user_id: int
    verified_at: datetime


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

    async def proved(
        self, *, guild_id: int, discord_user_id: int, newer_than: datetime | None = None
    ) -> ProvedAccount | None:
        """The account this person proved they hold, or None if they never proved one.

        How recent counts as recent is the caller's policy, and there are two of them. Unbinding a
        repository wants a proof from minutes ago, because the row permits something irreversible
        and holding an account an hour ago says little about now. Asking whether a link was ever
        proved at all wants no bound: the question is whether anybody ever vouched for it, and a
        proof from last year answers that as well as one from this morning.

        Filtering here rather than handing back a stale row is what stops the first caller acting
        on the second caller's answer.
        """
        wanted = select(VerifiedIdentity).where(
            VerifiedIdentity.discord_guild_id == guild_id,
            VerifiedIdentity.discord_user_id == discord_user_id,
        )
        if newer_than is not None:
            wanted = wanted.where(VerifiedIdentity.verified_at >= newer_than)

        row = await self._session.scalar(wanted)
        if row is None:
            return None
        return ProvedAccount(
            login=row.github_login,
            github_user_id=row.github_user_id,
            verified_at=row.verified_at,
        )
