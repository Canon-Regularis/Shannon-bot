from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import UserLink

logger = logging.getLogger(__name__)


class UserLinkStore:
    """Data access for the GitHub login to Discord account mapping, scoped to one guild.

    Logins are stored lowercased for the same reason as in item_assignments: GitHub treats
    them case insensitively.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> dict[str, int]:
        """Which of these people this server has a Discord account for.

        Keyed by login and answered by id. A login is not an identity: GitHub frees one when it
        is renamed or deleted and lets anybody take it, so a row matched on the name alone points
        at whoever holds it now rather than at the person somebody linked. Left that way, a
        stranger who took a freed name inherited the previous holder's mention everywhere a
        mention is built, including the review ping, which notifies them for work they were never
        asked to do.

        A row whose id disagrees with the person being asked about is not that person, so it is
        left out and they are named in plain text, which is what somebody who has never linked
        gets. Said out loud, because the two look identical in the thread and only one of them is
        somebody's link having gone stale.

        Either side not knowing an id falls back to the name, which is what happened before. A
        stored null is a row written before the column existed; an asked null is a payload that
        carried no id. Neither is evidence of anything, and refusing on no evidence would take
        away mentions that work.
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

    async def link(
        self,
        *,
        guild_id: int,
        github_username: str,
        github_user_id: int | None,
        discord_user_id: int,
    ) -> UserLink:
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
            # Both halves already point at each other. The id is still worth writing: a row from
            # before that column existed carries none, and re-running `/link` is the only way one
            # ever gets an answer, since GitHub can say what a login is called now and not what
            # it was called when somebody bound it.
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
