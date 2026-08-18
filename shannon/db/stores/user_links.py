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

        Both halves are unique within a guild and can be held by two different rows at once, so
        editing one in place collides with the other. Clear both, then insert.

        The clear and the insert have to be one step. An upsert cannot make them one: a row can
        conflict on either constraint and `ON CONFLICT` names a single one. Nor can a retry loop,
        since the retries collide with each other and not only with the original winner. Hence
        the advisory lock, held to the end of the transaction and keyed per guild so servers do
        not wait on one another. Nothing else here takes advisory locks, so the guild id alone
        is a safe key.
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
