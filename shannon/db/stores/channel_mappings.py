from __future__ import annotations

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shannon.db.models import ChannelMapping
from shannon.domain.enums import ObjectType


class ChannelMappingStore:
    """Data access for the channel each object type is posted into."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, repository_id: int, object_type: ObjectType) -> ChannelMapping | None:
        found: ChannelMapping | None = await self._session.scalar(
            select(ChannelMapping).where(
                ChannelMapping.repository_id == repository_id,
                ChannelMapping.object_type == object_type,
            )
        )
        return found

    async def set(
        self, *, repository_id: int, object_type: ObjectType, discord_channel_id: int
    ) -> ChannelMapping:
        """Point a type at a channel, whether or not one was mapped before.

        The insert settles the conflict itself: two people running /set_channel at once would
        both find nothing and both insert, and the second would hit the unique constraint.
        """
        statement = (
            pg_insert(ChannelMapping)
            .values(
                repository_id=repository_id,
                object_type=object_type,
                discord_channel_id=discord_channel_id,
            )
            .on_conflict_do_update(
                constraint="uq_channel_mappings_repo_type",
                # onupdate only fires for an ORM update, and this never goes through one.
                set_={"discord_channel_id": discord_channel_id, "updated_at": func.now()},
            )
            .returning(ChannelMapping)
        )
        return (await self._session.scalars(statement)).one()

    async def pin(
        self, *, repository_id: int, object_type: ObjectType, copying: ObjectType
    ) -> ChannelMapping | None:
        """Give a kind a row of its own at the channel it has been borrowing. Issue #134.

        `/register` maps pull requests and nothing else, so issue threads open in the pull
        request channel until somebody maps one for them. That makes where issues go a thing
        derived from the pull request row rather than recorded, and re-pointing pull requests
        took every issue thread along with them. This writes down what was already true, so
        that the two stop being the same answer.

        `DO NOTHING` rather than the `DO UPDATE` above, and the difference is load-bearing. A
        relocation stops at its cap and tells the admin to run `/set_channel` again; an update
        would re-pin on that second run, to the channel just set, and drag every issue after
        all — the same bug, surfacing only on the second invocation. It also settles the race
        with `/set_channel issues` running at the same moment, where an explicit choice already
        inserted has to win over this one.

        Answers with the row it wrote, or `None` in the two cases where it wrote nothing: the
        kind already had a channel of its own, or the kind being copied has none, which means
        there are no threads anywhere to preserve.
        """
        borrowed = select(
            literal(repository_id),
            literal(object_type.value),
            ChannelMapping.discord_channel_id,
        ).where(
            ChannelMapping.repository_id == repository_id,
            ChannelMapping.object_type == copying,
        )
        statement = (
            pg_insert(ChannelMapping)
            .from_select(["repository_id", "object_type", "discord_channel_id"], borrowed)
            .on_conflict_do_nothing(constraint="uq_channel_mappings_repo_type")
            .returning(ChannelMapping)
        )
        return (await self._session.scalars(statement)).one_or_none()
