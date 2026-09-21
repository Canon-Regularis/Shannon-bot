"""The three tables the GitHub App work adds, against a real Postgres.

Issue #98. Two of them decide who is allowed to destroy a binding and one decides which token a
request carries, so the cases worth writing down are the awkward ones: an App moved between
accounts, a link clicked twice, a proof that has gone stale.

Integration rather than unit throughout, because every interesting thing here is something the
database does. `consume` is one statement whose whole purpose is to settle a race; asserting that
against a fake would be asserting that the fake settles races.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import GitHubInstallation, IdentityVerification
from shannon.db.stores.identities import IdentityVerificationStore, VerifiedIdentityStore
from shannon.db.stores.installations import InstallationStore
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
BOB = 444
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
# The expiry is computed by the database, so a link is issued with a lifetime rather than a moment:
# `consume` expires a row against `now()`, and stamping it from here would compare two clocks.
LIVE = timedelta(minutes=10)


class TestWhichInstallationCoversAnOwner:
    async def test_an_account_that_was_never_installed_is_not_known(
        self, db_session: AsyncSession
    ) -> None:
        """None means "ask GitHub" rather than "not installed", which is the whole reason this
        table can be fed by webhooks with no reconciliation behind it."""
        assert await InstallationStore(db_session).for_owner("octocat") is None

    async def test_what_was_remembered_comes_back(self, db_session: AsyncSession) -> None:
        store = InstallationStore(db_session)

        await store.remember(installation_id=42, account_login="octocat", account_id=583231)

        found = await store.for_owner("octocat")
        assert found is not None
        assert (found.installation_id, found.account_id) == (42, 583231)

    async def test_the_login_is_matched_whatever_case_it_arrives_in(
        self, db_session: AsyncSession
    ) -> None:
        """GitHub echoes back whatever case a payload was written with, so a lookup respecting
        case would miss its own row about half the time."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="OctoCat")

        assert await store.for_owner("octocat") is not None
        assert await store.for_owner("OCTOCAT") is not None

    async def test_reinstalling_replaces_the_old_installation_id(
        self, db_session: AsyncSession
    ) -> None:
        """Uninstall and install again and GitHub keeps the login and issues a NEW id. Settling
        only on the id would leave the login pointing at an installation that no longer exists,
        and the next mint against it would fail with nothing to say why."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat")

        await store.remember(installation_id=99, account_login="octocat")

        found = await store.for_owner("octocat")
        assert found is not None
        assert found.installation_id == 99
        rows = (await db_session.scalars(select(GitHubInstallation))).all()
        assert len(rows) == 1, "the old row was left behind and the login is no longer unique"

    async def test_an_app_transferred_to_another_account_follows_it(
        self, db_session: AsyncSession
    ) -> None:
        """The other direction: the id survives and the login changes. The row has to follow, or
        the old login goes on resolving to a token that no longer covers it."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat")

        await store.remember(installation_id=42, account_login="hubot")

        assert await store.for_owner("octocat") is None
        assert await store.for_owner("hubot") is not None

    async def test_a_payload_with_no_account_id_does_not_erase_one_already_known(
        self, db_session: AsyncSession
    ) -> None:
        """The id is the only thing that tells a rename apart from somebody taking a freed name,
        so a less informative delivery must not overwrite a more informative one."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat", account_id=583231)

        await store.remember(installation_id=42, account_login="octocat")

        found = await store.for_owner("octocat")
        assert found is not None
        assert found.account_id == 583231

    async def test_forgetting_says_whether_there_was_anything_to_forget(
        self, db_session: AsyncSession
    ) -> None:
        """GitHub sends `installation.deleted` to every subscriber, including one that never held
        a row. A log line claiming to have removed nothing is worse than none."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat")

        assert await store.forget(42) is True
        assert await store.forget(42) is False

    async def test_suspending_keeps_the_row(self, db_session: AsyncSession) -> None:
        """Suspended and uninstalled are different answers. One means somebody turned the App off
        and can turn it back on; deleting the row would report it as never installed."""
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat")

        await store.remember(installation_id=42, account_login="octocat", suspended=True)

        found = await store.for_owner("octocat")
        assert found is not None
        assert found.suspended is True

    async def test_resuming_puts_it_back(self, db_session: AsyncSession) -> None:
        store = InstallationStore(db_session)
        await store.remember(installation_id=42, account_login="octocat", suspended=True)

        await store.remember(installation_id=42, account_login="octocat", suspended=False)

        found = await store.for_owner("octocat")
        assert found is not None
        assert found.suspended is False

    async def test_suspending_something_that_is_not_there_writes_the_row(
        self, db_session: AsyncSession
    ) -> None:
        """A suspend for an installation this bot never recorded is ordinary: the App may have
        been installed while the process was down. The row is written rather than skipped, so the
        next lookup says the App is off instead of never installed."""
        store = InstallationStore(db_session)

        await store.remember(installation_id=999, account_login="octocat", suspended=True)

        found = await store.for_owner("octocat")
        assert found is not None
        assert found.suspended is True


class TestTwoWritersOfOneAccount:
    """A reinstall and a transfer can land together, and neither may lose to the other.

    `remember` clears the row holding the login and upserts on the installation id, which are
    two constraints and two statements. Run twice at once without a lock, the clear from one
    lands between the other's clear and its insert, and the insert then conflicts on the
    constraint `ON CONFLICT` does not name: an `IntegrityError` out of a webhook handler, or two
    writers deadlocked against each other.

    GitHub sends `installation.created`, `installation.deleted` and `installation_repositories`
    for one account in the same second, and the worker's batch is worked in order, so the
    writers that collide are the worker and an inline directory refresh.
    """

    async def test_they_take_turns_rather_than_collide(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async def remember(installation_id: int, first: asyncio.Event | None) -> None:
            async with db_sessionmaker() as session, session.begin():
                await InstallationStore(session).remember(
                    installation_id=installation_id, account_login="octocat"
                )
                if first is not None:
                    first.set()
                    # Held open, so the second writer is still inside the lock when the
                    # assertion below asks Postgres whether anybody is waiting.
                    await asyncio.sleep(0.3)

        holding = asyncio.Event()
        one = asyncio.create_task(remember(42, holding))
        await holding.wait()
        other = asyncio.create_task(remember(99, None))

        await blocked_on_a_row(db_sessionmaker, other)
        await asyncio.gather(one, other)

        async with db_sessionmaker() as session:
            rows = (await session.scalars(select(GitHubInstallation))).all()
        assert [row.installation_id for row in rows] == [99], (
            "the second writer did not replace the first, or both rows survived"
        )

    async def test_two_accounts_do_not_wait_for_each_other(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other half: the lock is per account, so the App being installed on one
        organisation cannot hold up a webhook about another."""
        async with db_sessionmaker() as session:
            taken = await session.scalar(
                select(func.pg_advisory_xact_lock(8_532, func.hashtext("octocat")))
            )
            assert taken is None, "the lock call itself failed"

        async def remember(installation_id: int, login: str) -> None:
            async with db_sessionmaker() as session, session.begin():
                await InstallationStore(session).remember(
                    installation_id=installation_id, account_login=login
                )

        await asyncio.wait_for(
            asyncio.gather(remember(42, "octocat"), remember(99, "hubot")), timeout=10
        )

        async with db_sessionmaker() as session:
            rows = (await session.scalars(select(GitHubInstallation))).all()
        assert {row.account_login for row in rows} == {"octocat", "hubot"}


class TestTheOneTimeLink:
    async def test_a_link_can_be_spent_once(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(state="s1", guild_id=GUILD, discord_user_id=ALICE, lifetime=LIVE)

        assert await store.consume("s1") == (GUILD, ALICE)

    async def test_and_not_twice(self, db_session: AsyncSession) -> None:
        """The whole reason `consume` is one statement. A read followed by a write leaves a window
        where two clicks both see an unspent row, and the loser unbinds a repository on a link
        that had already been used."""
        store = IdentityVerificationStore(db_session)
        await store.issue(state="s1", guild_id=GUILD, discord_user_id=ALICE, lifetime=LIVE)
        await store.consume("s1")

        assert await store.consume("s1") is None

    async def test_an_expired_link_cannot_be_spent(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(state="s1", guild_id=GUILD, discord_user_id=ALICE, lifetime=-LIVE)

        assert await store.consume("s1") is None

    async def test_a_state_nobody_issued_cannot_be_spent(self, db_session: AsyncSession) -> None:
        assert await IdentityVerificationStore(db_session).consume("invented") is None

    async def test_spending_one_link_leaves_another_alone(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(state="s1", guild_id=GUILD, discord_user_id=ALICE, lifetime=LIVE)
        await store.issue(state="s2", guild_id=GUILD, discord_user_id=BOB, lifetime=LIVE)

        await store.consume("s1")

        assert await store.consume("s2") == (GUILD, BOB)

    async def test_a_spent_link_is_stamped_rather_than_deleted(
        self, db_session: AsyncSession
    ) -> None:
        """Kept so that a second click can be told apart from a state that never existed, which is
        the difference between a helpful page and a confusing one."""
        store = IdentityVerificationStore(db_session)
        await store.issue(state="s1", guild_id=GUILD, discord_user_id=ALICE, lifetime=LIVE)

        await store.consume("s1")

        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == "s1")
        )
        assert row is not None
        assert row.consumed_at is not None

    async def test_pruning_drops_links_long_past_use(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="old", guild_id=GUILD, discord_user_id=ALICE, lifetime=timedelta(days=-3)
        )
        await store.issue(state="live", guild_id=GUILD, discord_user_id=BOB, lifetime=LIVE)

        removed = await store.prune(keep_for=timedelta(days=1))

        assert removed == 1
        assert await store.consume("live") == (GUILD, BOB)

    async def test_pruning_an_empty_table_removes_nothing(self, db_session: AsyncSession) -> None:
        assert await IdentityVerificationStore(db_session).prune(keep_for=timedelta(days=1)) == 0


class TestWhatGitHubVouchedFor:
    async def test_somebody_who_never_proved_anything_has_nothing(
        self, db_session: AsyncSession
    ) -> None:
        found = await VerifiedIdentityStore(db_session).fresh(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None

    async def test_a_recent_proof_comes_back_as_the_login(self, db_session: AsyncSession) -> None:
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW,
        )

        found = await store.fresh(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found == "octocat"

    async def test_a_stale_proof_reads_as_none_rather_than_as_an_old_row(
        self, db_session: AsyncSession
    ) -> None:
        """Filtered here rather than handed back with a date on it. A caller given a stale row has
        to remember to check it, and the one that forgets is the one that unbinds a repository on
        a proof from last year."""
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW - timedelta(hours=2),
        )

        found = await store.fresh(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None

    async def test_proving_again_replaces_the_earlier_proof(self, db_session: AsyncSession) -> None:
        """Somebody moving between GitHub accounts overwrites cleanly, and only the latest one can
        permit anything anyway."""
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW - timedelta(hours=2),
        )

        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="hubot",
            github_user_id=100,
            verified_at=NOW,
        )

        found = await store.fresh(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )
        assert found == "hubot"

    async def test_one_person_proving_says_nothing_about_another(
        self, db_session: AsyncSession
    ) -> None:
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW,
        )

        found = await store.fresh(
            guild_id=GUILD, discord_user_id=BOB, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None

    async def test_proving_in_one_server_says_nothing_about_another(
        self, db_session: AsyncSession
    ) -> None:
        """A bot in two servers is two conversations. Somebody proving who they are where they
        have admin has said nothing about a server they merely happen to be in."""
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW,
        )

        found = await store.fresh(
            guild_id=2, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None
