"""The record of who asked this bot not to notify them. Issue #80.

The store answers one question for every message that names somebody, so the shape of the answer
matters more than usual: it is handed to Discord as the whole of what that message may notify, and
an answer read the wrong way round would either ping the people who asked not to be pinged or
silence everybody else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.db.models import MutedMember
from shannon.db.stores.muted_members import MutedMemberStore
from shannon.db.stores.user_links import UserLinkStore
from tests.integration.test_query_behaviour import QueryLog

pytestmark = pytest.mark.integration

ALICE = 555
BOB = 444


async def test_a_member_nobody_muted_may_be_pinged(db_session: AsyncSession) -> None:
    """No row is the state every member is in before they have ever heard of the command, so it
    has to mean what they have today rather than needing a backfill to say so."""
    store = MutedMemberStore(db_session)

    assert await store.may_be_pinged(guild_id=1, ids=[ALICE, BOB]) == (BOB, ALICE)


async def test_a_muted_member_is_left_out(db_session: AsyncSession) -> None:
    store = MutedMemberStore(db_session)
    await store.mute(guild_id=1, discord_user_id=ALICE)

    assert await store.may_be_pinged(guild_id=1, ids=[ALICE, BOB]) == (BOB,)


async def test_the_answer_is_who_may_be_pinged_not_who_may_not(db_session: AsyncSession) -> None:
    """The polarity, on its own, because both answers are tuples of ids and a reader who has the
    sense of it backwards writes code that passes every test about one muted person.

    Two muted and one not, so the two possible answers have different lengths as well as different
    contents and no accident of arithmetic can make them agree.
    """
    store = MutedMemberStore(db_session)
    await store.mute(guild_id=1, discord_user_id=ALICE)
    await store.mute(guild_id=1, discord_user_id=BOB)

    answer = await store.may_be_pinged(guild_id=1, ids=[ALICE, BOB, 777])

    assert answer == (777,), "it answered with the people who asked to be left alone"


async def test_the_answer_is_sorted(db_session: AsyncSession) -> None:
    """So the tests above can write a literal rather than a set, and so a failure reads as a
    difference in who rather than a difference in order."""
    store = MutedMemberStore(db_session)

    assert await store.may_be_pinged(guild_id=1, ids=[9, 3, 7]) == (3, 7, 9)


async def test_a_name_asked_about_twice_is_answered_once(db_session: AsyncSession) -> None:
    store = MutedMemberStore(db_session)

    assert await store.may_be_pinged(guild_id=1, ids=[ALICE, ALICE]) == (ALICE,)


async def test_asking_about_nobody_asks_the_database_nothing(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Every message with nobody to mention still comes through here, and most of them have
    nobody: a tag line, a state header, every thread a backlog mirror opens. None of those should
    cost a query, and the guard that makes sure of it is one `if` somebody could tidy away."""
    store = MutedMemberStore(db_session)

    log = QueryLog(db_engine)
    try:
        assert await store.may_be_pinged(guild_id=1, ids=[]) == ()
    finally:
        log.close()

    assert log.touching("muted_members") == []


async def test_muting_twice_is_not_an_error(db_session: AsyncSession) -> None:
    """Somebody clicking twice a second apart is ordinary, and answering it with a constraint
    violation would fail the command for doing nothing."""
    store = MutedMemberStore(db_session)

    await store.mute(guild_id=1, discord_user_id=ALICE)
    await store.mute(guild_id=1, discord_user_id=ALICE)

    assert await db_session.scalar(select(func.count()).select_from(MutedMember)) == 1


async def test_unmuting_somebody_who_was_never_muted_is_not_an_error(
    db_session: AsyncSession,
) -> None:
    """Which is what `/mentions on` is for most of the people who ever run it."""
    store = MutedMemberStore(db_session)

    await store.unmute(guild_id=1, discord_user_id=ALICE)

    assert await store.is_muted(guild_id=1, discord_user_id=ALICE) is False


async def test_unmuting_lets_them_be_pinged_again(db_session: AsyncSession) -> None:
    store = MutedMemberStore(db_session)
    await store.mute(guild_id=1, discord_user_id=ALICE)

    await store.unmute(guild_id=1, discord_user_id=ALICE)

    assert await store.may_be_pinged(guild_id=1, ids=[ALICE]) == (ALICE,)
    assert await store.is_muted(guild_id=1, discord_user_id=ALICE) is False


async def test_muting_here_says_nothing_about_another_server(db_session: AsyncSession) -> None:
    """A bot in two servers is two separate conversations, and somebody drowning in one of them
    is not asking to go quiet in the other."""
    store = MutedMemberStore(db_session)
    await store.mute(guild_id=1, discord_user_id=ALICE)

    assert await store.may_be_pinged(guild_id=2, ids=[ALICE]) == (ALICE,)
    assert await store.is_muted(guild_id=2, discord_user_id=ALICE) is False


async def test_running_link_again_does_not_forget_it(db_session: AsyncSession) -> None:
    """The whole reason this is its own table rather than a column on `user_links`.

    That row is cleared and rewritten on every `/link`, because either half of it may be held by a
    different row, and the warning about a login changing hands tells the person to run `/link`
    again. A preference kept there would be wiped by the one action the bot asks for by name, and
    the member would be pinged again with nothing anywhere saying why.
    """
    links = UserLinkStore(db_session)
    await links.link(guild_id=1, github_username="hubot", github_user_id=100, discord_user_id=ALICE)
    await MutedMemberStore(db_session).mute(guild_id=1, discord_user_id=ALICE)

    await links.link(guild_id=1, github_username="hubot", github_user_id=200, discord_user_id=ALICE)

    assert await MutedMemberStore(db_session).is_muted(guild_id=1, discord_user_id=ALICE) is True
