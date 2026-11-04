"""`/linked` from the command down to the rows it reads. Issue #232.

The command tests beside this one drive the callback against a stub, which pins the words. This
pins what a stub cannot: that the command the bot installs reads the link, the proof and the
repository through the container the bot actually builds, follows whether that deployment can run
`/link` at all, and asks GitHub nothing on the way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

import discord
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shannon.config import Settings
from shannon.container import Container
from shannon.db.models import Repository, UserLink
from shannon.db.stores.identities import VerifiedIdentityStore
from shannon.db.stores.user_links import UserLinkStore
from tests.fakes.discord_objects import FakeGuildPermissions, FakeInteraction, FakeMember
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support.commands import runnable
from tests.support.signing import SECRET
from tests.support.stack import build_stack

pytestmark = pytest.mark.integration

ALICE = 555
SIGNED_IN = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)

# A deployment where `/link` can run: the App's identity half, a URL to come back to, and
# Discord's half to say who is holding the browser.
SIGN_IN_SET_UP = Settings(
    github_webhook_secret=SecretStr(SECRET),
    github_app_client_id="Iv23liAbC",
    github_app_client_secret=SecretStr("shh"),
    public_base_url="https://shannon.example.com",
    discord_client_id="1300000000000000000",
    discord_client_secret=SecretStr("discord-shh"),
)

Run = Callable[[discord.Interaction], Awaitable[None]]


def stack(engine: AsyncEngine, *, github: FakeGitHubClient | None = None) -> Container:
    return build_stack(engine, threads=FakeThreadGateway(), github=github, settings=SIGN_IN_SET_UP)


async def ask(container: Container, *, guild_id: int = 1) -> str:
    """The real command out of the real container, run by a member holding no role."""
    command = next(c for c in runnable(container.commands) if c.name == "linked")
    interaction = FakeInteraction(
        user=FakeMember(id=ALICE, roles=[], guild_permissions=FakeGuildPermissions()),
        guild_id=guild_id,
    )
    await cast(Run, command.callback)(cast(discord.Interaction, interaction))
    return interaction.said


async def link(
    session: AsyncSession, login: str, github_user_id: int | None, *, guild_id: int = 1
) -> str:
    """Link Alice as `/link` would, and answer the day the reply should name."""
    await UserLinkStore(session).link(
        guild_id=guild_id,
        github_username=login,
        github_user_id=github_user_id,
        discord_user_id=ALICE,
    )
    await session.commit()
    made = await session.scalar(
        select(UserLink.created_at).where(UserLink.discord_guild_id == guild_id)
    )
    assert made is not None
    return f"<t:{int(made.timestamp())}:D>"


async def sign_in(
    session: AsyncSession, login: str, github_user_id: int, *, guild_id: int = 1
) -> None:
    """A round trip finished as this account, whatever it was for."""
    await VerifiedIdentityStore(session).remember(
        guild_id=guild_id,
        discord_user_id=ALICE,
        github_login=login,
        github_user_id=github_user_id,
        verified_at=SIGNED_IN,
    )
    await session.commit()


async def test_a_link_github_vouched_for_is_named_with_its_day_and_the_repository(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    day = await link(db_session, "octocat", 583231)
    await sign_in(db_session, "octocat", 583231)

    said = await ask(stack(db_engine))

    assert said == (
        f"You have been linked to octocat on GitHub since {day}, and GitHub has vouched that it "
        "is yours. If you have renamed your GitHub account since, run /link so this server "
        f"knows.\n\n-# This server mirrors {registered.repo_name}, and its threads mention you "
        "where GitHub names octocat."
    )


async def test_signing_in_later_as_another_account_leaves_the_link_unproved(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Every round trip writes the proof, so signing in as somebody else for /register replaced
    the one that stood behind the link."""
    await link(db_session, "octocat", 583231)
    await sign_in(db_session, "monalisa", 200)

    said = await ask(stack(db_engine))

    assert "but you last signed in to GitHub from here as monalisa" in said
    assert "Run /link and sign in as octocat to prove it" in said


async def test_a_name_that_changed_hands_is_told_apart_from_another_account(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await link(db_session, "octocat", 583231)
    await sign_in(db_session, "OctoCat", 999)

    said = await ask(stack(db_engine))

    assert "but the OctoCat you last signed in to GitHub from here as is a different account" in (
        said
    )
    assert "Run /link to link the account you hold now." in said


async def test_a_link_by_name_alone_is_told_to_run_link(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    day = await link(db_session, "octocat", None)

    said = await ask(stack(db_engine))

    assert said.startswith(f"You have been linked to octocat on GitHub by name alone since {day}")
    assert "Run /link to settle it." in said


async def test_signing_in_without_linking_is_not_being_linked(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The case the command exists for: GitHub knows them from /register, and they believe
    that made them linked."""
    await sign_in(db_session, "octocat", 583231)

    said = await ask(stack(db_engine))

    assert said.startswith(
        "You are not linked to a GitHub account in this server, although you last signed in to "
        "GitHub from here as octocat. Only /link links an account"
    )


async def test_somebody_who_never_linked_and_holds_no_role_is_told_how(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    said = await ask(stack(db_engine))

    assert said.startswith(
        "You are not linked to a GitHub account in this server. Run /link to connect yours:"
    )


async def test_where_sign_in_is_not_set_up_nobody_is_sent_to_link(
    registered: Repository, db_engine: AsyncEngine
) -> None:
    """The settings `/link` refuses without are what this reads, through the same service, so
    the two cannot disagree about whether a link can be made here."""
    container = build_stack(db_engine, threads=FakeThreadGateway())

    said = await ask(container)

    assert said.startswith(
        "You are not linked to a GitHub account in this server. This bot cannot link anybody "
        "until an admin finishes setting up sign-in with GitHub and Discord."
    )


async def test_a_link_in_another_server_is_not_a_link_here(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await link(db_session, "octocat", 583231, guild_id=2)
    await sign_in(db_session, "octocat", 583231, guild_id=2)

    said = await ask(stack(db_engine))

    assert said.startswith("You are not linked to a GitHub account in this server.")


async def test_a_server_with_no_repository_still_answers(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """/link asks for no repository, and /unregister keeps links, so this has to answer in a
    server that mirrors nothing."""
    day = await link(db_session, "octocat", 583231)

    said = await ask(stack(db_engine))

    assert said.startswith(f"You have been linked to octocat on GitHub since {day}")
    assert said.endswith("\n\n-# This server mirrors no repository yet.")


async def test_it_asks_github_nothing(
    registered: Repository, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Everything it says is already stored. A lookup on the way would make somebody who only
    wanted to know wait on GitHub, and fail them when GitHub is down. Every method of the fake
    raises its `error`, so any call at all fails this rather than only the ones listed."""
    await link(db_session, "octocat", 583231)
    await sign_in(db_session, "octocat", 583231)
    github = FakeGitHubClient()
    github.error = AssertionError("/linked asked GitHub something")

    said = await ask(stack(db_engine, github=github))

    assert said.startswith("You have been linked to octocat on GitHub since")
