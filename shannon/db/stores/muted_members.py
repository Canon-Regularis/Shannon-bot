"""Who this bot may notify, for the members who asked it not to.

Read on the way out of every message that names somebody, and written by one command nobody but
the member themselves runs. A row means that member asked to be left alone in that server; no row
means they never asked, which is what everybody was before this existed.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import MutedMember


class MutedMemberStore:
    """The members of one guild who have turned their own pings off."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def may_be_pinged(self, *, guild_id: int, ids: Iterable[int]) -> tuple[int, ...]:
        """Which of these Discord accounts this bot is still allowed to notify.

        Answers who MAY rather than who may not, because the answer is handed straight to Discord
        as the whole of what one message is permitted to notify. A list that had to be inverted on
        the way there is one somebody eventually inverts twice.

        Asked with the ids a message is about to name rather than with the guild alone, so the
        query is bounded by the item and not by how many people in the server have ever run the
        command. An empty set of ids asks the database nothing, which is the ordinary case for a
        backlog mirror and for a rebuild from a superseded delivery: both render the people in
        plain text, so there is nobody to allow and nothing to look up.

        Sorted, so a test asserting on the answer can write a literal instead of a set. Deduped,
        which costs nothing and is not needed today, because `user_links` is unique on the Discord
        account within a guild so one item's mentions cannot repeat one.

        No cap. Discord refuses more than a hundred entries here, and every caller is far under:
        GitHub allows ten assignees and fifteen requested reviewers, and a comment mentions at
        most ten. A path that resolved a whole server would be the first to need one.
        """
        wanted = set(ids)
        if not wanted:
            return ()

        muted = set(
            (
                await self._session.scalars(
                    select(MutedMember.discord_user_id).where(
                        MutedMember.discord_guild_id == guild_id,
                        MutedMember.discord_user_id.in_(wanted),
                    )
                )
            ).all()
        )
        return tuple(sorted(wanted - muted))

    async def is_muted(self, *, guild_id: int, discord_user_id: int) -> bool:
        """Whether this member has asked to be left alone here."""
        found = await self._session.scalar(
            select(MutedMember.id).where(
                MutedMember.discord_guild_id == guild_id,
                MutedMember.discord_user_id == discord_user_id,
            )
        )
        return found is not None

    async def mute(self, *, guild_id: int, discord_user_id: int) -> None:
        """Record that they want to be left alone, however many times they say so.

        The insert settles a repeat itself rather than reading first. Somebody clicking twice a
        second apart is ordinary and must not be answered with an error about a constraint.
        """
        await self._session.execute(
            pg_insert(MutedMember)
            .values(discord_guild_id=guild_id, discord_user_id=discord_user_id)
            .on_conflict_do_nothing(constraint="uq_muted_members_guild_discord")
        )

    async def unmute(self, *, guild_id: int, discord_user_id: int) -> None:
        """Let them be notified again. Matching nothing is the answer for somebody who never
        asked to be left alone, not a failure to report."""
        await self._session.execute(
            delete(MutedMember).where(
                MutedMember.discord_guild_id == guild_id,
                MutedMember.discord_user_id == discord_user_id,
            )
        )
