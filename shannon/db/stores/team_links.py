from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import TeamLink


class TeamLinkStore:
    """Data access for the GitHub team to Discord role mapping, scoped to one guild.

    Slugs are stored lowercased, because GitHub normalises a team slug to lowercase itself.
    `resolve_many` is named as the user store names it, so a caller can take either.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> dict[str, int]:
        """Which of these teams this server has a role for.

        The mapping holds the names asked for a review, people and teams together, and the ids
        beside them are for the account store. GitHub numbers teams separately, so a team has no
        id there and the slug is the only thing this can match on.
        """
        wanted = {name.lower() for name in people}
        if not wanted:
            return {}

        rows = (
            await self._session.scalars(
                select(TeamLink).where(
                    TeamLink.discord_guild_id == guild_id,
                    TeamLink.github_team.in_(wanted),
                )
            )
        ).all()
        return {row.github_team: row.discord_role_id for row in rows}

    async def link(self, *, guild_id: int, github_team: str, discord_role_id: int) -> TeamLink:
        """Point a team at a role, replacing whatever that team pointed at before.

        Only the team half is unique: two teams sharing a role is allowed, so there is nothing to
        race on and no advisory lock to take, unlike linking a person.
        """
        slug = github_team.strip().lstrip("@").lower()
        row = await self._session.scalar(
            pg_insert(TeamLink)
            .values(
                discord_guild_id=guild_id,
                github_team=slug,
                discord_role_id=discord_role_id,
            )
            .on_conflict_do_update(
                constraint="uq_team_links_guild_team",
                # `updated_at` by hand: SQLAlchemy fires `TimestampMixin`'s `onupdate` for
                # an UPDATE it built, not for the `set_` of an upsert.
                set_={"discord_role_id": discord_role_id, "updated_at": func.now()},
            )
            .returning(TeamLink)
        )
        await self._session.flush()
        # An upsert that updates on conflict always returns its row, which `scalar`'s return
        # type cannot say. Asserted rather than branched on, as `db.base.rows_changed` explains.
        assert row is not None
        return row
