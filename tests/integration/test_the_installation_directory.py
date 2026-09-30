"""Turning an owner into an installation id, out of the database.

Issue #98. Thin on purpose, and the one decision in it is what a suspended installation answers.
It is the seam the token minter sits on, so the cases are: known, unknown, and turned off.

Three answers rather than two now, and that is the point of the type. "Unknown" was always
documented to mean "ask GitHub" rather than "not installed", and the minter finally does ask - so
a suspended row answering the same None as a missing one stopped being free. It meant the minter
asked GitHub about a paused App on every single call and wrote the row back unchanged each time.
It writes as well as reads for the same reason: an answer nothing can keep is asked for again.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.github.installations import InstallationDirectory, Knows, MapSays

pytestmark = pytest.mark.integration


async def test_an_owner_nobody_installed_on_is_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    found = await InstallationDirectory(db_sessionmaker).installation_for("stranger")

    assert found == MapSays(Knows.NOTHING)


async def test_an_installed_owner_resolves_to_its_installation(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await InstallationStore(db_session).remember(installation_id=42, account_login="octocat")
    await db_session.commit()

    found = await InstallationDirectory(db_sessionmaker).installation_for("octocat")

    assert found == MapSays(Knows.AN_INSTALLATION, 42)


async def test_the_owner_is_matched_whatever_case_the_caller_used(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The owner arrives off a webhook payload or a parsed link, and GitHub is careless about case
    in both. A directory that missed its own row half the time would send every other request out
    unauthenticated, which reads downstream as the repository having vanished."""
    await InstallationStore(db_session).remember(installation_id=42, account_login="octocat")
    await db_session.commit()

    found = await InstallationDirectory(db_sessionmaker).installation_for("OctoCat")

    assert found == MapSays(Knows.AN_INSTALLATION, 42)


async def test_a_suspended_installation_is_a_suspension_rather_than_nothing(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Somebody turned the App off. Minting against it fails, so there is nothing to gain by
    trying, and the row is kept rather than deleted so this stays distinguishable from an App
    nobody ever installed.

    Distinguishable to the CALLER, which is the part that was missing. Both used to answer None,
    so the minter could not tell "I have nothing, go and ask" from "I have this and it is paused",
    and went and asked about the paused one on every call for as long as it stayed paused.
    """
    store = InstallationStore(db_session)
    await store.remember(installation_id=42, account_login="octocat")
    await store.remember(installation_id=42, account_login="octocat", suspended=True)
    await db_session.commit()

    found = await InstallationDirectory(db_sessionmaker).installation_for("octocat")

    assert found == MapSays(Knows.A_SUSPENSION)


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

    found = await InstallationDirectory(db_sessionmaker).installation_for("octocat")

    assert found == MapSays(Knows.AN_INSTALLATION, 42)


async def test_an_installation_learnt_from_github_is_written_down(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of `for_owner` answering None to mean "ask GitHub". Something now
    asks, and this is where the answer lands: without it the ask happens on every call for
    ever, which is one extra request per event to learn the same fact again.
    """
    directory = InstallationDirectory(db_sessionmaker)

    await directory.remember(
        installation_id=42, account_login="OctoCat", account_id=7, suspended=False
    )

    assert await directory.installation_for("octocat") == MapSays(Knows.AN_INSTALLATION, 42)


async def test_writing_one_down_twice_is_the_same_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two deliveries for one owner can miss the map together and both recover it."""
    directory = InstallationDirectory(db_sessionmaker)

    for _ in range(2):
        await directory.remember(
            installation_id=42, account_login="octocat", account_id=None, suspended=False
        )

    assert await directory.installation_for("octocat") == MapSays(Knows.AN_INSTALLATION, 42)


async def test_a_suspended_installation_learnt_from_github_is_kept_and_refused(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Kept, because the App being installed here is worth knowing, and refused, because minting
    against a suspended installation fails.

    Kept is what stops the asking. The row read back as `A_SUSPENSION` is the whole return on
    writing it: the minter reads that and stops, where before it read None and asked GitHub again.
    """
    directory = InstallationDirectory(db_sessionmaker)

    await directory.remember(
        installation_id=42, account_login="octocat", account_id=None, suspended=True
    )

    assert await directory.installation_for("octocat") == MapSays(Knows.A_SUSPENSION)
    async with db_sessionmaker() as session:
        found = await InstallationStore(session).for_owner("octocat")
    assert found is not None and found.suspended


async def test_forgetting_one_makes_the_owner_a_question_again(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """What a 404 on the mint needs. A row naming an installation GitHub no longer has answers
    `AN_INSTALLATION`, which shuts discovery out - so every later call posts a mint that 404s,
    and a 404 mints no token, so nothing caches the refusal. Dropping the row turns the next call
    back into a question, which is the one thing this module can now answer.
    """
    directory = InstallationDirectory(db_sessionmaker)
    await directory.remember(
        installation_id=42, account_login="octocat", account_id=None, suspended=False
    )

    await directory.forget(42)

    assert await directory.installation_for("octocat") == MapSays(Knows.NOTHING)


async def test_forgetting_one_nobody_wrote_down_is_quiet(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A 404 can arrive for an installation the webhook already removed. Two callers racing to
    forget the same one is the ordinary case, not an error."""
    directory = InstallationDirectory(db_sessionmaker)

    await directory.forget(42)

    assert await directory.installation_for("octocat") == MapSays(Knows.NOTHING)
