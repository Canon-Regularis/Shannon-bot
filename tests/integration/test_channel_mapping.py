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
from shannon.services.sync.policies import channel_fallbacks
from tests.support.db import register_repository

pytestmark = pytest.mark.integration


@pytest.fixture
def channels(db_sessionmaker: async_sessionmaker) -> ChannelMappingService:
    """Wired the way the container wires it, fallbacks and all.

    Built bare, this answers "nothing was mapped before" for a kind that has been landing in
    another kind's channel all along, and no test could see the difference.
    """
    return ChannelMappingService(db_sessionmaker, channel_fallbacks())


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


class TestAKindThatHasBeenBorrowingAnotherChannel:
    """`/register` maps pull requests and nothing else, so until somebody runs `/set_channel
    issues`, issue threads open in the pull request channel. That is the shipped default and
    the commonest state a server is in.

    What the answer has to get right is where the threads already open are. Discord cannot move
    a thread between channels, so every item already tracked keeps the one it has, and an admin
    who is not told where that is goes looking for threads that never went anywhere.
    """

    async def test_the_first_mapping_names_the_channel_they_were_borrowing(
        self, db_session: AsyncSession, channels: ChannelMappingService
    ) -> None:
        # `register_repository` maps pull requests and nothing else, which is exactly what
        # `/register` does and exactly the state this is about.
        await register_repository(db_session, channel_id=100)

        assignment = await channels.assign(guild_id=1, object_type=ObjectType.ISSUE, channel_id=200)

        assert assignment.replaced == 100, "it answered from a row nothing was ever placed by"

    async def test_a_kind_with_no_fallback_still_replaces_nothing(
        self, registered: Repository, channels: ChannelMappingService
    ) -> None:
        """Tickets have none on purpose: an unmapped board mirrors nothing rather than
        appearing uninvited in the pull request channel."""
        assignment = await channels.assign(
            guild_id=1, object_type=ObjectType.TICKET, channel_id=500
        )

        assert assignment.replaced is None
