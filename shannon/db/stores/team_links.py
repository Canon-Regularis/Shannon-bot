from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import TeamLink


class TeamLinkStore:
    """Data access for the GitHub team to Discord role mapping, scoped to one guild.

    Slugs are stored lowercased, as logins are next door: GitHub normalises a team slug to
    lowercase itself, and matching case sensitively would only make a hand-typed `Backend-Team`
    fail to find the row it just wrote.

    Answers with `resolve_many` under the same name the user store uses, so whatever renders a
    ping can take either without knowing which it has.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_many(
        self, *, guild_id: int, people: Mapping[str, int | None]
    ) -> dict[str, int]:
        """Which of these teams this server has a role for.

        The parameter is named for people because the caller cannot tell the two apart and
        should not have to: it holds the names that were asked for a review, and this answers
        for the ones it recognises.

        The ids beside them are for the account store, which uses them to tell a login that has
        changed hands from the person somebody linked. A team has no id in that space: GitHub
        numbers teams separately, and `mapping.team` carries a slug and nothing else. So they are
        taken and ignored here, and the slug is the only thing this can match on.
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

        Simpler than linking a person, because only one half is unique. A team maps to one role
        and the insert settles its own conflict on that; two teams sharing a role is allowed and
        needs no clearing, so there is nothing here to race on and no advisory lock to take.
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
                # `updated_at` by hand. `TimestampMixin` carries an `onupdate`, and SQLAlchemy
                # fires that for an UPDATE it built, not for the `set_` of an upsert, so without
                # this a team pointed at a new role keeps the timestamp of the first one.
                set_={"discord_role_id": discord_role_id, "updated_at": func.now()},
            )
            .returning(TeamLink)
        )
        await self._session.flush()
        return row
