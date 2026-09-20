from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import UserLink

logger = logging.getLogger(__name__)


class UserLinkStore:
    """Data access for the GitHub login to Discord account mapping, scoped to one guild.

    Logins are stored lowercased because GitHub treats them case insensitively.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> dict[str, int]:
        """Which of these people this server has a Discord account for, keyed by login.

        GitHub frees a renamed or deleted login for anybody to take, so a name-only match once gave
        a stranger the previous holder's review pings. A stored account id that disagrees with the
        one on the item is dropped and logged, since a stale link and a member who never linked look
        identical in the thread. A null on either side is no evidence — the row predates the column,
        or the payload carried no id — so the name still resolves.
        """
        wanted = {name.lower(): asked for name, asked in people.items()}
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

        resolved: dict[str, int] = {}
        for row in rows:
            asked = wanted[row.github_username]
            if row.github_user_id is not None and asked is not None and row.github_user_id != asked:
                logger.warning(
                    "%r is linked here to the account %s, and the one on this item is %s, so it "
                    "is somebody else now and will not be mentioned; that person should run "
                    "/link again",
                    row.github_username,
                    row.github_user_id,
                    asked,
                )
                continue
            resolved[row.github_username] = row.discord_user_id
        return resolved

    async def login_for(self, *, guild_id: int, discord_user_id: int) -> str | None:
        """Which GitHub account this Discord member claimed here, or None if they never did.

        Unlike `resolve_many` there is no second account id to hold the stored one against, so this
        answers with the claim as it was made; a stale link asks the wrong person for a review, and
        GitHub still applies its own rules about who may go on an item. At most one row matches:
        `uq_user_links_guild_discord` is unique on this pair and its index serves the lookup.
        """
        found = await self._session.scalar(
            select(UserLink.github_username).where(
                UserLink.discord_guild_id == guild_id,
                UserLink.discord_user_id == discord_user_id,
            )
        )
        # A scalar off a column comes back untyped, and a login is the one thing this may promise.
        return found if isinstance(found, str) else None

    async def logins_for(
        self, *, guild_id: int, discord_user_ids: Collection[int]
    ) -> dict[int, str]:
        """Which GitHub account each of these Discord members claimed here, keyed by Discord id.

        `login_for` for a batch, and as lenient: with no item payload to supply an account id, a
        login somebody freed and a stranger took is answered with the stranger. An id nobody linked
        is absent from the result.
        """
        wanted = set(discord_user_ids)
        if not wanted:
            return {}

        rows = (
            await self._session.execute(
                select(UserLink.discord_user_id, UserLink.github_username).where(
                    UserLink.discord_guild_id == guild_id,
                    UserLink.discord_user_id.in_(wanted),
                )
            )
        ).all()
        return {discord_user_id: login for discord_user_id, login in rows}

    async def link(
        self,
        *,
        guild_id: int,
        github_username: str,
        github_user_id: int | None,
        discord_user_id: int,
    ) -> UserLink:
        """Bind a GitHub login to a Discord account, replacing whatever either side had.

        Both halves are unique within a guild and can be held by two different rows, so editing one
        in place collides with the other: clear both and insert, as one step. An upsert cannot do it
        — a row can conflict on either constraint and `ON CONFLICT` names one — and nor can a retry
        loop, since the retries collide with each other. Hence the advisory lock, keyed per guild;
        the bare guild id is safe because the project's other lock passes Postgres two integers
        rather than one bigint, and those spaces stay apart.
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
            # The id is still worth writing: a row from before that column carries none, and
            # re-running `/link` is the only way one is ever filled in, since GitHub can say what a
            # login is called now and not what it was called when somebody bound it.
            existing.github_user_id = github_user_id
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
            github_user_id=github_user_id,
            discord_user_id=discord_user_id,
        )
        self._session.add(link)
        await self._session.flush()
        return link
