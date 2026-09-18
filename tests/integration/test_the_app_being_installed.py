"""The App being installed, removed, paused and resumed, through the handler that records it.

Issue #98. These deliveries decide whether this bot can read a repository at all, so the cases
that matter are the awkward middles: a suspend for an account nothing ever recorded, a delete for
one that was never installed here, and a repository being dropped from an installation that still
exists.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.installations import InstallationStore
from shannon.github.webhooks.events import WebhookOutcome
from shannon.github.webhooks.installations import build_installation_handler

pytestmark = pytest.mark.integration


def delivery(**overrides: object) -> dict[str, object]:
    installation: dict[str, object] = {
        "id": 42,
        "account": {"login": "octocat", "id": 583231, "type": "User"},
    }
    installation.update(overrides)
    return {"installation": installation}


async def stored(session: AsyncSession, login: str = "octocat"):
    return await InstallationStore(session).for_owner(login)


class TestInstalling:
    async def test_installing_writes_the_account_down(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)

        outcome = await handle("created", delivery())

        assert outcome is WebhookOutcome.PROCESSED
        found = await stored(db_session)
        assert found is not None
        assert (found.installation_id, found.account_id) == (42, 583231)

    async def test_it_is_not_suspended_to_begin_with(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)

        await handle("created", delivery())

        found = await stored(db_session)
        assert found is not None
        assert found.suspended is False

    async def test_accepting_new_permissions_keeps_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """An ordinary event when the App's permissions change. It carries the whole installation,
        so the simplest correct thing is to write it down again."""
        handle = build_installation_handler(db_sessionmaker)
        await handle("created", delivery())

        outcome = await handle("new_permissions_accepted", delivery())

        assert outcome is WebhookOutcome.PROCESSED
        assert await stored(db_session) is not None

    async def test_a_repository_added_to_an_installation_keeps_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)

        await handle("added", delivery())

        assert await stored(db_session) is not None

    async def test_a_repository_removed_does_not_forget_the_installation(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The distinction that matters. Taking one repository out of an installation leaves the
        installation standing, and forgetting it would break every other repository under that
        account at the same time."""
        handle = build_installation_handler(db_sessionmaker)
        await handle("created", delivery())

        await handle("removed", delivery())

        assert await stored(db_session) is not None


class TestRemoving:
    async def test_uninstalling_forgets_the_account(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)
        await handle("created", delivery())

        outcome = await handle("deleted", delivery())

        assert outcome is WebhookOutcome.PROCESSED
        assert await stored(db_session) is None

    async def test_uninstalling_something_never_installed_here_is_not_an_error(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """GitHub sends this to every subscriber. Answering anything but "handled" would put the
        delivery through sixteen retries to reach the same conclusion."""
        handle = build_installation_handler(db_sessionmaker)

        with caplog.at_level(logging.INFO):
            outcome = await handle("deleted", delivery())

        assert outcome is WebhookOutcome.PROCESSED
        assert "not installed on here" in caplog.text


class TestPausing:
    async def test_suspending_keeps_the_row_and_marks_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)
        await handle("created", delivery())

        await handle("suspend", delivery())

        found = await stored(db_session)
        assert found is not None
        assert found.suspended is True

    async def test_resuming_clears_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)
        await handle("created", delivery())
        await handle("suspend", delivery())

        await handle("unsuspend", delivery())

        found = await stored(db_session)
        assert found is not None
        assert found.suspended is False

    async def test_a_suspend_for_an_account_never_seen_still_leaves_a_row(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Which happens whenever the App was installed while this process was down. Writing the
        row and then marking it means the state is right either way round."""
        handle = build_installation_handler(db_sessionmaker)

        await handle("suspend", delivery())

        found = await stored(db_session)
        assert found is not None
        assert found.suspended is True


class TestADeliveryThatSaysNothingUsable:
    async def test_a_payload_with_no_installation_is_ignored(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Every delivery from a repository webhook configured by hand looks like this, and they
        are ordinary during the changeover."""
        handle = build_installation_handler(db_sessionmaker)

        outcome = await handle("created", {"action": "created"})

        assert outcome is WebhookOutcome.IGNORED

    async def test_it_is_said_out_loud(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        handle = build_installation_handler(db_sessionmaker)

        with caplog.at_level(logging.WARNING):
            await handle("created", {"installation": {"id": 42}})

        assert "without a usable installation" in caplog.text
