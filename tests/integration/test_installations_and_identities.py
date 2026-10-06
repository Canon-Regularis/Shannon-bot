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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import GitHubInstallation, IdentityVerification
from shannon.db.stores.identities import (
    IdentityVerificationStore,
    PendingLink,
    ProvedAccount,
    SpentLink,
    VerifiedIdentityStore,
)
from shannon.db.stores.installations import InstallationStore
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
BOB = 444
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
# The expiry is computed by the database, so a link is issued with a lifetime rather than a moment:
# `consume` expires a row against `now()`, and stamping it from here would compare two clocks.
LIVE = timedelta(minutes=10)
# What a row holds once a browser has proved itself through Discord. The store compares it and does
# nothing else with it, so any string serves; the service is what makes it a keyed hash.
BOUND = "a-browser-that-proved-itself"


async def spend(store: IdentityVerificationStore, state: str) -> SpentLink | None:
    """Spend a link the way the service does: bound first, by the member it was issued for.

    Found reviewing #201: a link nothing has bound cannot be spent at all, so every test here of
    what spending answers binds first. The member is read off the row, because what is under test
    in those is the spending; `TestOnlyTheBrowserThatProvedItSpendsALink` is about the binding.
    """
    pending = await store.pending(state)
    if pending is not None:
        await store.bind(state, discord_user_id=pending.discord_user_id, binding=BOUND)
    return await store.consume(state, binding=BOUND)


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
        holding, let_go = asyncio.Event(), asyncio.Event()

        async def remember(installation_id: int, holds: bool) -> None:
            async with db_sessionmaker() as session, session.begin():
                await InstallationStore(session).remember(
                    installation_id=installation_id, account_login="octocat"
                )
                if holds:
                    holding.set()
                    await let_go.wait()

        one = asyncio.create_task(remember(42, holds=True))
        await holding.wait()
        other = asyncio.create_task(remember(99, holds=False))

        # Held until the wait is observed rather than for a fixed moment: a sleep long enough
        # on an idle machine is not long enough on a loaded one, and the helper answers
        # "nothing ever blocked" rather than passing, which is the right way round but still
        # a failure that says nothing about the lock.
        await blocked_on_a_row(db_sessionmaker, other)
        let_go.set()
        await asyncio.gather(one, other)

        async with db_sessionmaker() as session:
            rows = (await session.scalars(select(GitHubInstallation))).all()
        assert [row.installation_id for row in rows] == [99], (
            "the second writer did not replace the first, or both rows survived"
        )

    async def test_another_account_is_not_held_up_by_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other half, and the half a lock taken on nothing in particular would fail.

        Every GitHub call resolves an installation, so serialising the whole table would put
        one organisation's webhooks behind whichever other account is slowest to write.
        """
        holding, let_go = asyncio.Event(), asyncio.Event()

        async def hold_octocat() -> None:
            async with db_sessionmaker() as session, session.begin():
                await InstallationStore(session).remember(
                    installation_id=42, account_login="octocat"
                )
                holding.set()
                await let_go.wait()

        held = asyncio.create_task(hold_octocat())
        await holding.wait()

        async with db_sessionmaker() as session, session.begin():
            await asyncio.wait_for(
                InstallationStore(session).remember(installation_id=99, account_login="hubot"),
                timeout=5,
            )

        let_go.set()
        await held

        async with db_sessionmaker() as session:
            rows = (await session.scalars(select(GitHubInstallation))).all()
        assert {row.account_login for row in rows} == {"octocat", "hubot"}


class TestTheOneTimeLink:
    async def test_a_link_can_be_spent_once(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )

        assert await spend(store, "s1") == SpentLink(GUILD, ALICE, VerificationPurpose.LINK)

    async def test_and_not_twice(self, db_session: AsyncSession) -> None:
        """The whole reason `consume` is one statement. A read followed by a write leaves a window
        where two clicks both see an unspent row, and the loser unbinds a repository on a link
        that had already been used."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )
        await spend(store, "s1")

        assert await spend(store, "s1") is None

    async def test_an_expired_link_cannot_be_spent(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=-LIVE,
        )

        assert await spend(store, "s1") is None

    async def test_a_state_nobody_issued_cannot_be_spent(self, db_session: AsyncSession) -> None:
        assert await spend(IdentityVerificationStore(db_session), "invented") is None

    async def test_spending_one_link_leaves_another_alone(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )
        await store.issue(
            state="s2",
            guild_id=GUILD,
            discord_user_id=BOB,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )

        await spend(store, "s1")

        assert await spend(store, "s2") == SpentLink(GUILD, BOB, VerificationPurpose.LINK)

    async def test_a_spent_link_is_stamped_rather_than_deleted(
        self, db_session: AsyncSession
    ) -> None:
        """Kept so that a second click can be told apart from a state that never existed, which is
        the difference between a helpful page and a confusing one."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )

        await spend(store, "s1")

        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == "s1")
        )
        assert row is not None
        assert row.consumed_at is not None

    async def test_pruning_drops_links_long_past_use(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="old",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=timedelta(days=-3),
        )
        await store.issue(
            state="live",
            guild_id=GUILD,
            discord_user_id=BOB,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )

        removed = await store.prune(keep_for=timedelta(days=1))

        assert removed == 1
        assert await spend(store, "live") == SpentLink(GUILD, BOB, VerificationPurpose.LINK)

    async def test_pruning_an_empty_table_removes_nothing(self, db_session: AsyncSession) -> None:
        assert await IdentityVerificationStore(db_session).prune(keep_for=timedelta(days=1)) == 0


class TestOnlyTheBrowserThatProvedItSpendsALink:
    """Found reviewing #201. The state alone used to spend a link, so whoever was forwarded one
    finished it as the member it was issued for. Now a link is spent only with the binding Discord
    let one browser write - and these are the database's half of that."""

    async def issued(
        self,
        store: IdentityVerificationStore,
        *,
        state: str = "s1",
        lifetime: timedelta = LIVE,
    ) -> None:
        await store.issue(
            state=state,
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=lifetime,
            board=ChosenBoard(number=6),
        )

    async def test_a_link_nothing_has_bound_cannot_be_spent(self, db_session: AsyncSession) -> None:
        """Null is what every link handed out before this holds, and it equals nothing."""
        store = IdentityVerificationStore(db_session)
        await self.issued(store)

        assert await store.consume("s1", binding=BOUND) is None
        assert await store.pending("s1") is not None, "a refused spend used the link up"

    async def test_another_browser_cannot_spend_it(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await self.issued(store)
        await store.bind("s1", discord_user_id=ALICE, binding=BOUND)

        assert await store.consume("s1", binding="another-browser") is None
        assert await store.consume("s1", binding=BOUND) == SpentLink(
            GUILD, ALICE, VerificationPurpose.BOARD, ChosenBoard(number=6, owner="")
        )

    async def test_only_the_member_it_was_issued_for_can_bind_it(
        self, db_session: AsyncSession
    ) -> None:
        """Guarded in the statement as well as by the service's comparison, so nothing but
        Discord naming THIS member can ever write a binding."""
        store = IdentityVerificationStore(db_session)
        await self.issued(store)

        assert await store.bind("s1", discord_user_id=BOB, binding=BOUND) is False
        assert await store.consume("s1", binding=BOUND) is None

    async def test_binding_again_moves_it_to_the_newer_browser(
        self, db_session: AsyncSession
    ) -> None:
        """The same member opening their link on a second device. Only they can bind it at all,
        so the last browser they proved themselves in is the one that may finish."""
        store = IdentityVerificationStore(db_session)
        await self.issued(store)
        await store.bind("s1", discord_user_id=ALICE, binding="first-browser")

        assert await store.bind("s1", discord_user_id=ALICE, binding="second-browser") is True
        assert await store.consume("s1", binding="first-browser") is None
        assert await store.consume("s1", binding="second-browser") is not None

    async def test_a_spent_link_cannot_be_bound_again(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await self.issued(store)
        await store.bind("s1", discord_user_id=ALICE, binding=BOUND)
        await store.consume("s1", binding=BOUND)

        assert await store.bind("s1", discord_user_id=ALICE, binding="later") is False

    async def test_an_expired_link_cannot_be_bound(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await self.issued(store, lifetime=-LIVE)

        assert await store.bind("s1", discord_user_id=ALICE, binding=BOUND) is False

    async def test_a_state_nobody_issued_cannot_be_bound(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)

        assert await store.bind("invented", discord_user_id=ALICE, binding=BOUND) is False

    async def test_reading_a_pending_link_changes_nothing(self, db_session: AsyncSession) -> None:
        """Asked on the way INTO the round trip, which a link preview can start as easily as a
        person can - so it must leave the row exactly as it found it."""
        store = IdentityVerificationStore(db_session)
        await self.issued(store)

        first = await store.pending("s1")
        second = await store.pending("s1")

        assert first == second == PendingLink(GUILD, ALICE, VerificationPurpose.BOARD)
        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == "s1")
        )
        assert row is not None
        assert (row.bound_browser, row.consumed_at) == (None, None)

    async def test_a_spent_expired_or_invented_link_is_not_pending(
        self, db_session: AsyncSession
    ) -> None:
        store = IdentityVerificationStore(db_session)
        await self.issued(store, state="spent")
        await self.issued(store, state="expired", lifetime=-LIVE)
        await spend(store, "spent")

        assert await store.pending("spent") is None
        assert await store.pending("expired") is None
        assert await store.pending("invented") is None


class TestWhatGitHubVouchedFor:
    async def test_somebody_who_never_proved_anything_has_nothing(
        self, db_session: AsyncSession
    ) -> None:
        found = await VerifiedIdentityStore(db_session).proved(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None

    async def test_a_recent_proof_comes_back_as_the_account(self, db_session: AsyncSession) -> None:
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW,
        )

        found = await store.proved(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found == ProvedAccount(login="octocat", github_user_id=583231, verified_at=NOW)

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

        found = await store.proved(
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

        found = await store.proved(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )
        assert found == ProvedAccount(login="hubot", github_user_id=100, verified_at=NOW)

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

        found = await store.proved(
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

        found = await store.proved(
            guild_id=2, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None


class TestAProofThatDoesNotGoStale:
    """The same row read without a window, which is a different question. Issue #133 follow-up.

    Unbinding a repository asks whether somebody is at the keyboard now. Asking whether a stored
    link was ever more than somebody's say-so is not time-limited: a proof from last year answers
    it as well as one from this morning, and treating it as expired would quietly mark every
    long-standing member unproved.
    """

    async def test_a_proof_far_past_any_window_still_answers(
        self, db_session: AsyncSession
    ) -> None:
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW - timedelta(days=400),
        )

        assert await store.proved(guild_id=GUILD, discord_user_id=ALICE) == ProvedAccount(
            login="octocat", github_user_id=583231, verified_at=NOW - timedelta(days=400)
        )

    async def test_the_same_row_reads_as_stale_when_a_window_is_given(
        self, db_session: AsyncSession
    ) -> None:
        """Both answers off one row, which is what makes the two callers safe to keep apart."""
        store = VerifiedIdentityStore(db_session)
        await store.remember(
            guild_id=GUILD,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=583231,
            verified_at=NOW - timedelta(days=400),
        )

        found = await store.proved(
            guild_id=GUILD, discord_user_id=ALICE, newer_than=NOW - timedelta(minutes=15)
        )

        assert found is None

    async def test_somebody_who_never_proved_anything_has_nothing_either_way(
        self, db_session: AsyncSession
    ) -> None:
        assert (
            await VerifiedIdentityStore(db_session).proved(guild_id=GUILD, discord_user_id=ALICE)
            is None
        )


class TestWhichCommandAskedForIt:
    """The callback is a browser arriving with nothing but a state, so the row is the only record
    of what the person was in the middle of. Issue #144."""

    async def test_the_purpose_survives_the_round_trip(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.UNREGISTER,
            lifetime=LIVE,
        )

        assert await spend(store, "s1") == SpentLink(GUILD, ALICE, VerificationPurpose.UNREGISTER)

    async def test_two_links_for_one_person_keep_their_own_purposes(
        self, db_session: AsyncSession
    ) -> None:
        """Somebody part way through one command can start the other, and spending either must
        not tell the page what the other was for."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="linking",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )
        await store.issue(
            state="unbinding",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.UNREGISTER,
            lifetime=LIVE,
        )

        first = await spend(store, "unbinding")
        second = await spend(store, "linking")

        assert first is not None and first.purpose is VerificationPurpose.UNREGISTER
        assert second is not None and second.purpose is VerificationPurpose.LINK


class TestWhichBoardALinkWasFor:
    """Issue #201. One link both authorises and links a board, so the board somebody chose has to
    come back out of the row the browser lands on. The URL carries nothing but the state, so the
    row is the only place it can come from - the same position the purpose above is in."""

    async def test_the_board_chosen_survives_the_round_trip(self, db_session: AsyncSession) -> None:
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=LIVE,
            board=ChosenBoard(number=6, owner="acme"),
        )

        assert await spend(store, "s1") == SpentLink(
            GUILD, ALICE, VerificationPurpose.BOARD, ChosenBoard(number=6, owner="acme")
        )

    async def test_an_owner_nobody_named_is_null_on_the_row_and_blank_again_after(
        self, db_session: AsyncSession
    ) -> None:
        """Every entry the picker offers is a bare number. Null in the column, which is how
        `repositories.project_owner` spells the same absence, and blank on the way out, which is
        how `ChosenBoard` does - so an empty string is never a third spelling of it."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=LIVE,
            board=ChosenBoard(number=6),
        )

        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == "s1")
        )
        assert row is not None
        assert (row.board_number, row.board_owner) == (6, None)
        spent = await spend(store, "s1")
        assert spent is not None
        assert spent.board == ChosenBoard(number=6, owner="")

    async def test_a_link_that_names_no_board_only_authorises(
        self, db_session: AsyncSession
    ) -> None:
        """Null for both, which is what every link handed out before the columns existed says, and
        what every identity link still says."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=LIVE,
        )

        spent = await spend(store, "s1")
        assert spent is not None
        assert spent.board is None

    async def test_the_tier_survives_the_round_trip(self, db_session: AsyncSession) -> None:
        """Found reviewing #201: the tier a board command was gated on, which following the link
        asks Discord about again. Sorted on the way in, so one set is always written one way."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=LIVE,
            tier=frozenset({"PROJECT_MANAGER", "ADMIN"}),
        )

        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == "s1")
        )
        assert row is not None
        assert row.tier == "ADMIN,PROJECT_MANAGER"
        spent = await spend(store, "s1")
        assert spent is not None
        assert spent.tier == frozenset({"ADMIN", "PROJECT_MANAGER"})

    async def test_a_link_with_no_tier_spends_as_none(self, db_session: AsyncSession) -> None:
        """Every identity link, and every board link from before the column."""
        store = IdentityVerificationStore(db_session)
        await store.issue(
            state="s1",
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LIVE,
        )

        spent = await spend(store, "s1")
        assert spent is not None
        assert spent.tier is None

    async def test_two_links_for_one_person_keep_their_own_boards(
        self, db_session: AsyncSession
    ) -> None:
        """The board belongs to the link rather than to the person. Somebody who asked for two
        boards and then follows the older link links the board that link was handed out for."""
        store = IdentityVerificationStore(db_session)
        for state, number in (("first", 6), ("second", 7)):
            await store.issue(
                state=state,
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                lifetime=LIVE,
                board=ChosenBoard(number=number),
            )

        first = await spend(store, "first")
        second = await spend(store, "second")

        assert first is not None and first.board == ChosenBoard(number=6)
        assert second is not None and second.board == ChosenBoard(number=7)
