from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import UserLink


class UserLinkStore:
    """Data access for the GitHub login to Discord account mapping, scoped to one guild.

    Logins are stored lowercased for the same reason as in item_assignments: GitHub treats
    them case insensitively.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, guild_id: int, github_username: str) -> UserLink | None:
        return await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.github_username == github_username.lower(),
            )
        )

    async def resolve_many(
        self, *, guild_id: int, github_usernames: Iterable[str]
    ) -> dict[str, int]:
        wanted = {name.lower() for name in github_usernames}
        if not wanted:
            return {}

        rows: Sequence[UserLink] = (
            await self._session.scalars(
                select(UserLink).where(
                    UserLink.discord_guild_id == guild_id,
                    UserLink.github_username.in_(wanted),
                )
            )
        ).all()
        return {row.github_username: row.discord_user_id for row in rows}

    async def link(self, *, guild_id: int, github_username: str, discord_user_id: int) -> UserLink:
        """Bind a GitHub login to a Discord account, replacing whatever either side had.

        Both halves are unique within a guild, and they can be held by two different rows at
        once. Editing one of those rows in place collides with the other, so anything holding
        either half is cleared out first and the pairing written fresh.

        That clearing and writing has to be one indivisible step, which is what the lock is for.
        Two of these overlapping both find nothing to clear and both insert, and the loser hits
        one of the two constraints. An upsert cannot settle it, because a row can conflict on
        either half and `ON CONFLICT` takes one constraint. Retrying cannot settle it either:
        that was tried, and it fails whenever the retries collide with each other rather than
        with the original winner, which a third caller makes likely and a slow machine makes
        ordinary. It was caught by a test that failed roughly one run in four.

        The lock is held to the end of the transaction and taken per guild, so linking in one
        server never waits on another. Nothing else in the schema uses advisory locks, so the
        guild id can be the whole key.
        """
        await self._session.execute(select(func.pg_advisory_xact_lock(guild_id)))

        username = github_username.lower()

        existing = await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.github_username == username,
                UserLink.discord_user_id == discord_user_id,
            )
        )
        if existing is not None:
            return existing

        await self._session.execute(
            delete(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                or_(
                    UserLink.github_username == username,
                    UserLink.discord_user_id == discord_user_id,
                ),
            )
        )

        link = UserLink(
            discord_guild_id=guild_id,
            github_username=username,
            discord_user_id=discord_user_id,
        )
        self._session.add(link)
        await self._session.flush()
        return link
