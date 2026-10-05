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
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose


@dataclass(frozen=True, slots=True)
class SpentLink:
    """Whose one-time link was just spent, and what spending it finishes.

    A value object rather than the tuple this used to be. Two ids and an enum read as nothing at
    all positionally, and the two ids are both integers, so a call site that swapped them would
    bind the wrong person and fail nowhere.
    """

    guild_id: int
    discord_user_id: int
    purpose: VerificationPurpose
    # The board a board link was handed out to link, or None to authorise only - which is every
    # other purpose, and every link that named no board. Issue #201. A default, so a link that
    # carries none reads exactly as it did before there was anything to carry.
    board: ChosenBoard | None = None


@dataclass(frozen=True, slots=True)
class PendingLink:
    """A link that can still be followed, and the member it was handed out for.

    Read on the way through Discord, before anything is spent: what is asked of Discord is whether
    the browser holding the link is held by `discord_user_id`, and nothing else on the row matters
    to that question. The guild rides along so a refusal can say where the link came from.
    """

    guild_id: int
    discord_user_id: int
    purpose: VerificationPurpose


class IdentityVerificationStore:
    """The one-time links the commands that need one hand out, and the single use of each."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self,
        *,
        state: str,
        guild_id: int,
        discord_user_id: int,
        purpose: VerificationPurpose,
        lifetime: timedelta,
        board: ChosenBoard | None = None,
    ) -> None:
        """Hand out a link that expires a fixed time from now.

        The moment is computed by the database, because `consume` expires a row against `now()`
        and a row stamped by the application would be compared against a different clock.

        The purpose is written now because it cannot be worked out later: the callback is a
        browser arriving with nothing but the state, and what it should say next depends on which
        command sent the person away.

        The board is written now for the same reason, where a board link was handed out to link
        one. Issue #201. Here rather than in the URL, so nobody can change it on the way.
        """
        await self._session.execute(
            pg_insert(IdentityVerification).values(
                state=state,
                discord_guild_id=guild_id,
                discord_user_id=discord_user_id,
                purpose=purpose,
                expires_at=func.now() + interval(lifetime),
                board_number=board.number if board else None,
                # A blank owner is "nobody named one", which the column spells as null, the way
                # `repositories.project_owner` spells the same absence.
                board_owner=(board.owner or None) if board else None,
            )
        )

    async def pending(self, state: str) -> PendingLink | None:
        """The link a state names, while it can still be followed, or None.

        Read-only, and on purpose: this is asked on the way INTO the round trip, which a link
        preview or a crawler can start as easily as a person can. Nothing is decided by it except
        whether there is anything to send somebody to Discord about.
        """
        found = (
            await self._session.execute(
                select(
                    IdentityVerification.discord_guild_id,
                    IdentityVerification.discord_user_id,
                    IdentityVerification.purpose,
                ).where(
                    IdentityVerification.state == state,
                    IdentityVerification.consumed_at.is_(None),
                    IdentityVerification.expires_at > func.now(),
                )
            )
        ).first()
        if found is None:
            return None
        return PendingLink(guild_id=found[0], discord_user_id=found[1], purpose=found[2])

    async def bind(self, state: str, *, discord_user_id: int, binding: str) -> bool:
        """Write down the browser Discord has just said is held by the member this link is for.

        Found reviewing #201. Guarded on the member as well as the state, so nothing but proof of
        being THIS member can bind it, and on the link still being followable. Binding again
        replaces the browser: the last browser that proved itself is the one that may finish,
        and only the issuer can prove anything, so that is the issuer changing their mind.

        Answers whether a row was bound. Nothing is, where the link expired or was spent in the
        moments between being read and being bound.
        """
        bound = await rows_changed(
            self._session,
            update(IdentityVerification)
            .where(
                IdentityVerification.state == state,
                IdentityVerification.discord_user_id == discord_user_id,
                IdentityVerification.consumed_at.is_(None),
                IdentityVerification.expires_at > func.now(),
            )
            .values(bound_browser=binding),
        )
        return bound == 1

    async def consume(self, state: str, *, binding: str) -> SpentLink | None:
        """Spend a link, answering whose it was and what it finishes, or None if it cannot be spent.

        Filter, stamp and answer in one statement, so two clicks on the same link race in
        Postgres and exactly one of them wins. One answer for expired, already used and never
        existed: telling them apart would confirm to somebody guessing states that a particular
        one was real.

        And only by the browser that proved itself through Discord. Found reviewing #201: the state
        alone used to spend a link, so whoever was forwarded one finished it as its issuer. A row
        nothing has bound holds null, which equals nothing, so a link handed out before this - or
        one whose holder never went through Discord - cannot be spent at all.
        """
        spent = await self._session.execute(
            update(IdentityVerification)
            .where(
                IdentityVerification.state == state,
                IdentityVerification.bound_browser == binding,
                IdentityVerification.consumed_at.is_(None),
                IdentityVerification.expires_at > func.now(),
            )
            .values(consumed_at=func.now())
            .returning(
                IdentityVerification.discord_guild_id,
                IdentityVerification.discord_user_id,
                IdentityVerification.purpose,
                IdentityVerification.board_number,
                IdentityVerification.board_owner,
            )
        )
        found = spent.first()
        if found is None:
            return None
        return SpentLink(
            guild_id=found[0],
            discord_user_id=found[1],
            purpose=found[2],
            # A null owner back to the blank `ChosenBoard` uses for "nobody named one".
            board=None if found[3] is None else ChosenBoard(number=found[3], owner=found[4] or ""),
        )

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

        What it does not filter on is WHICH command asked. `identity_verifications` records that
        and `redeem` writes the proof before it looks, so this table has no purpose column and a
        proof minted by one command satisfies another within its window. Deliberate rather than
        missed: what a proof carries is identity, and every authorisation built on one asks GitHub
        again about the repository in front of it. What a crossed proof costs is the deliberateness
        of having come back from the browser for this particular thing.
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
