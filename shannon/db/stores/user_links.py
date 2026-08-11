from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
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
        """Bind a GitHub login to a Discord account, replacing whatever either side had."""
        username = github_username.lower()

        by_discord = await self._session.scalar(
            select(UserLink).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.discord_user_id == discord_user_id,
            )
        )
        if by_discord is not None:
            by_discord.github_username = username
            await self._session.flush()
            return by_discord

        existing = await self.get(guild_id=guild_id, github_username=username)
        if existing is not None:
            existing.discord_user_id = discord_user_id
            await self._session.flush()
            return existing

        link = UserLink(
            discord_guild_id=guild_id,
            github_username=username,
            discord_user_id=discord_user_id,
        )
        self._session.add(link)
        await self._session.flush()
        return link
