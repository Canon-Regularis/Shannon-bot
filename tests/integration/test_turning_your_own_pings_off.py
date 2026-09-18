"""`/mentions` from the command down to what Discord is told. Issue #80.

The command tests beside this one drive the callback against a stub, which proves the wording and
the refusals. This proves the part a stub cannot: that running it writes something the sync path
reads, through the container the bot actually builds.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.user_links import UserLinkStore
from shannon.services.mentions import MentionPreferences
from tests.fakes.discord_objects import FakeGuildPermissions, FakeInteraction, FakeMember
from tests.fakes.threads import FakeThreadGateway
from tests.support.stack import build_stack

pytestmark = pytest.mark.integration

ALICE = 555


async def run_mentions(container, state: str | None, *, user_id: int = ALICE) -> str:
    """The real command out of the real container, with no roles on the member running it."""
    command = next(c for c in container.commands if c.name == "mentions")
    interaction = FakeInteraction(
        user=FakeMember(id=user_id, roles=[], guild_permissions=FakeGuildPermissions())
    )
    choice = None
    if state is not None:
        choice = next(c for c in command._params["state"].choices if c.value == state)
    await command.callback(interaction, choice)
    return interaction.reply


async def test_it_reports_on_before_anybody_has_ever_run_it(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """No row is the state every member starts in, and it has to read as the behaviour they
    already have rather than as an unanswered question."""
    container = build_stack(db_engine, threads=FakeThreadGateway())

    assert (await run_mentions(container, None)).startswith("Mentions are on.")


async def test_turning_them_off_and_asking_again_agrees(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    container = build_stack(db_engine, threads=FakeThreadGateway())

    await run_mentions(container, "off")

    assert (await run_mentions(container, None)).startswith("Mentions are off.")


async def test_turning_them_back_on_undoes_it(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    container = build_stack(db_engine, threads=FakeThreadGateway())
    await run_mentions(container, "off")

    await run_mentions(container, "on")

    assert (await run_mentions(container, None)).startswith("Mentions are on.")


async def test_running_it_twice_the_same_way_is_not_an_error(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """Clicking twice a second apart is ordinary. Answering it with a constraint violation would
    fail the command for doing nothing."""
    container = build_stack(db_engine, threads=FakeThreadGateway())

    await run_mentions(container, "off")
    await run_mentions(container, "off")

    assert (await run_mentions(container, None)).startswith("Mentions are off.")


async def test_one_member_turning_them_off_says_nothing_about_another(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    container = build_stack(db_engine, threads=FakeThreadGateway())

    await run_mentions(container, "off", user_id=ALICE)

    assert (await run_mentions(container, None, user_id=999)).startswith("Mentions are on.")


async def test_what_the_command_writes_is_what_the_sync_reads(
    registered: Repository,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    pr_event,
) -> None:
    """The whole point, in one test. Everything else here is about the command answering
    correctly; this is the command and the mirror agreeing about the same member.

    Driven through the container's own pull request sync, so the wiring that hands the notifier
    and the block their view of who may be pinged is the wiring under test rather than one this
    file assembled.
    """
    threads = FakeThreadGateway()
    container = build_stack(db_engine, threads=threads)
    await UserLinkStore(db_session).link(
        guild_id=1, github_username="monalisa", github_user_id=200, discord_user_id=ALICE
    )
    await db_session.commit()

    await run_mentions(container, "off")
    result = await container.pr_sync.sync(pr_event("opened"))

    assert f"<@{ALICE}>" in threads.metadata_of(result.thread_id), "she was dropped from the block"
    assert [notify for _, _, _, notify in threads.allowed] == [()]


async def test_the_service_answers_about_the_server_it_was_asked_about(
    registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A bot in two servers is two conversations, and somebody drowning in one is not asking to
    go quiet in the other."""
    preferences = MentionPreferences(db_sessionmaker)
    await preferences.set_mentions(guild_id=1, discord_user_id=ALICE, wanted=False)

    assert await preferences.wants_mentions(guild_id=1, discord_user_id=ALICE) is False
    assert await preferences.wants_mentions(guild_id=2, discord_user_id=ALICE) is True
