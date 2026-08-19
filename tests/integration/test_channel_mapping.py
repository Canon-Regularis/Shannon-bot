"""What /set_channel does to the database, and what it hands back to say so.

The service had been driven only by a concurrency test, which cared that three callers landing
together leave one mapping and never looked at what a single caller gets told. `replaced` is the
whole of that answer: it is how the reply distinguishes "issues now go here" from "issues have
moved here from somewhere else".
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ChannelMapping, Repository
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError
from shannon.services.channels import ChannelMappingService

pytestmark = pytest.mark.integration


@pytest.fixture
def channels(db_sessionmaker: async_sessionmaker) -> ChannelMappingService:
    return ChannelMappingService(db_sessionmaker)


async def test_a_type_with_no_channel_yet_replaces_nothing(
    registered: Repository, channels: ChannelMappingService, db_session: AsyncSession
) -> None:
    # The fixture maps PR and ISSUE, so the third type is the one with nowhere to go yet.
    assignment = await channels.assign(guild_id=1, object_type=ObjectType.TICKET, channel_id=500)

    assert assignment.replaced is None
    assert assignment.repository_name == registered.repo_name
    stored = await db_session.scalar(
        select(ChannelMapping).where(ChannelMapping.object_type == ObjectType.TICKET)
    )
    assert stored is not None
    assert stored.discord_channel_id == 500


async def test_moving_a_type_reports_where_it_came_from(
    registered: Repository, channels: ChannelMappingService, db_session: AsyncSession
) -> None:
    assignment = await channels.assign(guild_id=1, object_type=ObjectType.ISSUE, channel_id=501)

    assert assignment.replaced == 98
    assert assignment.discord_channel_id == 501
    mappings = (
        await db_session.scalars(
            select(ChannelMapping).where(ChannelMapping.object_type == ObjectType.ISSUE)
        )
    ).all()
    assert len(mappings) == 1, "moving a channel left the old mapping behind"


async def test_the_types_do_not_move_each_other(
    registered: Repository, channels: ChannelMappingService, db_session: AsyncSession
) -> None:
    await channels.assign(guild_id=1, object_type=ObjectType.ISSUE, channel_id=501)

    db_session.expire_all()
    pr_mapping = await db_session.scalar(
        select(ChannelMapping).where(ChannelMapping.object_type == ObjectType.PR)
    )
    assert pr_mapping is not None
    assert pr_mapping.discord_channel_id == 99


async def test_a_guild_with_no_repository_is_told_to_register_first(
    channels: ChannelMappingService, db_session: AsyncSession
) -> None:
    """The ordinary way round to get this wrong: /set_channel before /register."""
    with pytest.raises(NotRegisteredError, match="Run /register first"):
        await channels.assign(guild_id=404, object_type=ObjectType.ISSUE, channel_id=501)

    assert (await db_session.scalars(select(ChannelMapping))).all() == []
