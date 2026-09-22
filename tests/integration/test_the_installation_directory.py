"""Turning an owner into an installation id, out of the database.

Issue #98. Thin on purpose, and the one decision in it is what a suspended installation answers.
It is the seam the token minter sits on, so the cases are: known, unknown, and turned off.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.github.installations import InstallationDirectory

pytestmark = pytest.mark.integration


async def test_an_owner_nobody_installed_on_resolves_to_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert await InstallationDirectory(db_sessionmaker).installation_for("stranger") is None


async def test_an_installed_owner_resolves_to_its_installation(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await InstallationStore(db_session).remember(installation_id=42, account_login="octocat")
    await db_session.commit()

    found = await InstallationDirectory(db_sessionmaker).installation_for("octocat")

    assert found == 42


async def test_the_owner_is_matched_whatever_case_the_caller_used(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The owner arrives off a webhook payload or a parsed link, and GitHub is careless about case
    in both. A directory that missed its own row half the time would send every other request out
    unauthenticated, which reads downstream as the repository having vanished."""
    await InstallationStore(db_session).remember(installation_id=42, account_login="octocat")
    await db_session.commit()

    assert await InstallationDirectory(db_sessionmaker).installation_for("OctoCat") == 42


async def test_a_suspended_installation_resolves_to_nothing(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Somebody turned the App off. Minting against it fails, so there is nothing to gain by
    trying, and the row is kept rather than deleted so this stays distinguishable from an App
    nobody ever installed."""
    store = InstallationStore(db_session)
    await store.remember(installation_id=42, account_login="octocat")
    await store.remember(installation_id=42, account_login="octocat", suspended=True)
    await db_session.commit()

    assert await InstallationDirectory(db_sessionmaker).installation_for("octocat") is None


async def test_a_suspended_installation_says_so_rather_than_looking_uninstalled(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole reason the state is kept. Without the line, somebody pausing the App and then
    wondering why nothing mirrors has nothing anywhere to tell them what they did."""
    store = InstallationStore(db_session)
    await store.remember(installation_id=42, account_login="octocat")
    await store.remember(installation_id=42, account_login="octocat", suspended=True)
    await db_session.commit()

    with caplog.at_level(logging.INFO):
        await InstallationDirectory(db_sessionmaker).installation_for("octocat")

    assert "suspended" in caplog.text


async def test_resuming_makes_it_resolve_again(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    store = InstallationStore(db_session)
    await store.remember(installation_id=42, account_login="octocat", suspended=True)
    await store.remember(installation_id=42, account_login="octocat", suspended=False)
    await db_session.commit()

    assert await InstallationDirectory(db_sessionmaker).installation_for("octocat") == 42
