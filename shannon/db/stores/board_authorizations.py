"""The GitHub authorisations people grant so a server can reach their project board.

Rows only. The encryption, and the policy about what an unreadable row means, live in
`shannon.services.board_credentials` - this store never sees a plaintext token and has no opinion
about one. That split is on purpose: a store that could decrypt would be a second place where the
key has to be handled correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.base import rows_changed
from shannon.db.models import BoardAuthorization


@dataclass(frozen=True, slots=True)
class HeldAuthorization:
    """One stored authorisation, with the token still encrypted.

    Named for what it is rather than for what it becomes. A caller holding this has a row and not
    yet a credential, and whether it is ever a credential depends on the key.
    """

    secret: str
    github_login: str
    github_user_id: int


class BoardAuthorizationStore:
    """One row per person per server, replaced rather than duplicated when they authorise again."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def remember(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        github_login: str,
        github_user_id: int,
        secret: str,
    ) -> None:
        """Keep an authorisation, replacing whatever that person had in that server.

        An upsert on the one constraint, which is all that is needed here - unlike `user_links`,
        where both halves are unique within a guild and an upsert cannot name two constraints at
        once. Here the GitHub account is data rather than a key, so somebody who authorises a
        second account simply replaces the first: the newer grant is the one they meant.
        """
        login = github_login.strip()
        await self._session.execute(
            pg_insert(BoardAuthorization)
            .values(
                discord_guild_id=guild_id,
                discord_user_id=discord_user_id,
                github_login=login,
                github_user_id=github_user_id,
                secret=secret,
            )
            .on_conflict_do_update(
                constraint="uq_board_authorizations_guild_discord",
                set_={
                    "github_login": login,
                    "github_user_id": github_user_id,
                    "secret": secret,
                },
            )
        )

    async def held(self, *, guild_id: int, discord_user_id: int) -> HeldAuthorization | None:
        """What this person has on file in this server, or None."""
        row = await self._session.scalar(
            select(BoardAuthorization).where(
                BoardAuthorization.discord_guild_id == guild_id,
                BoardAuthorization.discord_user_id == discord_user_id,
            )
        )
        if row is None:
            return None
        return HeldAuthorization(
            secret=row.secret,
            github_login=row.github_login,
            github_user_id=row.github_user_id,
        )

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool:
        """Delete one authorisation, answering whether there was one.

        The answer matters to the caller: "it is gone" and "there was nothing" are different
        things to tell somebody who just asked for it to be taken away.
        """
        gone = await rows_changed(
            self._session,
            delete(BoardAuthorization).where(
                BoardAuthorization.discord_guild_id == guild_id,
                BoardAuthorization.discord_user_id == discord_user_id,
            ),
        )
        # Compared on one line rather than branched: the row count is bounded at one by the
        # constraint, so there is nothing a second arm could say.
        return gone > 0
