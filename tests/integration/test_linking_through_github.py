"""Following a one-time link is the whole of linking a GitHub account. Issue #144.

`/link` used to record a login somebody typed, which GitHub was never asked about, and `/verify`
existed to ask. Two commands, one of which could be wrong about who somebody is. Now there is one,
and the click finishes it: GitHub says which account signed in and the row is written there and
then, from the same answer the proof is taken from.

Driven against a real database and a real callback, because what makes this safe is not the
command. It is that the row the browser lands on says who the link was for and what it finishes,
and that the write happens where GitHub's answer is rather than where somebody's typing was.

The other half of the same callback is `test_unregistering.py`, which must keep NOT linking
anybody — that is the case this file exists to pin from the outside.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import BoardAuthorization, IdentityVerification, UserLink, VerifiedIdentity
from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.user_links import LinkedAccount, UserLinkStore
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose
from shannon.services.board_credentials import BoardCredentials
from shannon.services.linking import UserLinkingService
from shannon.services.verification import (
    BOARD_SCOPE,
    NO_APPLICATION,
    GitHubIdentityVerification,
    OAuthClient,
    VerificationError,
)
from tests.fakes.board_links import NoBoardLinks
from tests.fakes.tiers import FakeTiers
from tests.support.credentials import BOARD_KEY, OTHER_BOARD_KEY
from tests.support.db import register_repository
from tests.support.round_trip import DISCORD_APP, discord_says, round_trip, state_of, to_github

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
BOB = 777
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def github_says(*, login: str = "octocat", user_id: int = 583231, scope: object = BOARD_SCOPE):
    """A GitHub that completes the round trip and names one account.

    `scope` is what the token response says was granted, and by default it is everything a board
    asks for. Issue #201: a board is refused on less, so a GitHub that answered without it would
    be one no board in this file could be authorised through.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            return httpx.Response(
                200, content=json.dumps({"access_token": "gho_abc", "scope": scope})
            )
        return httpx.Response(200, content=json.dumps({"login": login, "id": user_id}))

    return handler


@asynccontextmanager
async def verifying(
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    keys: str = BOARD_KEY,
    discord: OAuthClient = DISCORD_APP,
) -> AsyncIterator[GitHubIdentityVerification]:
    """The real service over a real linking service, which is the point of this file.

    A stand-in for the linking half would prove the call was made and nothing about what it
    wrote, and what it writes is the whole question here.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(discord_says(handler))) as http:
        yield GitHubIdentityVerification(
            sessionmaker,
            UserLinkingService(sessionmaker),
            # Real, with a real key, for the same reason the linking service above is real: a
            # stand-in would prove the call was made and nothing about what landed in the row,
            # and whether the stored token is the token is the whole question for a board.
            BoardCredentials(sessionmaker, keys=keys),
            # No link in this file carries a board: linking one on the way back is
            # `test_one_click_board_linking.py`'s subject, with the real service behind it.
            board_links=NoBoardLinks(),
            tiers=FakeTiers(),
            client_id="Iv23liAbC",
            client_secret="shh",
            oauth_url="https://github.com",
            public_base_url="https://shannon.example.com",
            http=http,
            board=OAuthClient(
                client_id="Ov23liBoard", client_secret="board-shh", scope=BOARD_SCOPE
            ),
            discord=discord,
            now=lambda: NOW,
        )


async def followed(
    verification: GitHubIdentityVerification,
    *,
    purpose: VerificationPurpose,
    discord_user_id: int = ALICE,
) -> None:
    """Hand out a link for somebody and have them follow it, through Discord as themselves."""
    link = await verification.link_for(
        guild_id=GUILD, discord_user_id=discord_user_id, purpose=purpose
    )
    await round_trip(verification, link, member=discord_user_id)


class TestFollowingALinkLinksYou:
    async def test_the_row_is_written_by_the_click(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The whole of issue #144: no second command, and nothing typed."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_it_records_the_account_github_named_and_never_one_from_the_url(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The id as well as the name, off the same `GET /user` the proof is taken from. A login
        is a label GitHub reassigns; the id is the half that lasts."""
        async with verifying(
            db_sessionmaker, github_says(login="TheOctocat", user_id=999)
        ) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="theoctocat", github_user_id=999)

    async def test_the_proof_is_recorded_either_way(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The proof lands before anything is decided with it, and unconditionally. That ordering
        is what keeps the other half of this callback untouched."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.UNREGISTER)
            proved = await verification.ever_proved(guild_id=GUILD, discord_user_id=ALICE)

        assert proved is not None
        assert (proved.login, proved.github_user_id) == ("octocat", 583231)

    async def test_an_unregister_callback_writes_no_link(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The case the purpose column exists for. `/unregister` wants the proof and nothing
        else: it has an irreversible thing to do next and it needs somebody to report the answer
        to, so the browser page sends them back rather than finishing anything."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.UNREGISTER)

        assert (
            await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
            is None
        )

    async def test_it_is_written_for_whoever_the_link_was_issued_for(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The row carries the Discord account, and the link is finished for exactly that
        account - by them alone, since #201's review, because Discord has to name them first. Which
        is why nothing ever hands one out for somebody other than the person in front of it."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK, discord_user_id=BOB)

        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=GUILD, discord_user_id=BOB) is not None
        assert await store.account_for(guild_id=GUILD, discord_user_id=ALICE) is None


class TestAProofBeatsAClaim:
    async def test_it_takes_a_login_somebody_else_had_claimed(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Reached end to end for the first time. Somebody had typed this login against their own
        Discord account back when typing one was how linking worked; GitHub has now said whose it
        is, and the row that goes is the one nobody ever vouched for."""
        await UserLinkStore(db_session).link(
            guild_id=GUILD, github_username="octocat", github_user_id=583231, discord_user_id=BOB
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        db_session.expunge_all()
        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=GUILD, discord_user_id=ALICE) == LinkedAccount(
            login="octocat", github_user_id=583231
        )
        assert await store.account_for(guild_id=GUILD, discord_user_id=BOB) is None

    async def test_it_replaces_whatever_that_member_had_before(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The other way a wrong link is fixed, and the one that needs no admin: somebody bound to
        an account that was never theirs signs in as their own."""
        await UserLinkStore(db_session).link(
            guild_id=GUILD,
            github_username="somebody-else",
            github_user_id=111,
            discord_user_id=ALICE,
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_two_outstanding_links_both_work_and_the_last_one_wins(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Running the command twice before clicking leaves two live states. Both are spendable,
        and if the browser has changed GitHub account in between the second click wins — which is
        what a proof beating a claim means applied to two proofs."""
        async with verifying(db_sessionmaker, github_says()) as first:
            one = await first.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
            )
            two = await first.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
            )
            await round_trip(first, one, member=ALICE)

        async with verifying(db_sessionmaker, github_says(login="wanderer", user_id=900)) as then:
            await round_trip(then, two, member=ALICE)

        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="wanderer", github_user_id=900)
        assert await db_session.scalar(select(UserLink).where(UserLink.discord_user_id == ALICE))


class TestAuthorisingABoard:
    """Issue #170. The one purpose whose token is kept, and the properties that make that safe.

    Every other round trip in this file finishes by learning a name and drops the token on the way
    out. A board is read every couple of seconds with nobody at a keyboard, so its authorisation
    has to outlive the browser visit - which makes this the first credential this project stores,
    and the reason the column is encrypted.
    """

    async def test_the_link_asks_for_the_project_scope(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Read AND write, because moving a card is the feature on the other side of it. A
        `read:project` grant would leave every /status refusing a write nobody could fix."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            link = await verification.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.BOARD
            )
            at = await to_github(verification, link, member=ALICE)

        assert "scope=project" in at.url

    async def test_an_identity_link_still_asks_for_no_scope_at_all(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other arm, and a security property rather than a tidiness one. A GitHub App's user
        token with the default empty scope can call `GET /user`, which is the whole of what the
        other three purposes need - so asking for anything there would be asking for more than the
        job requires, on a link people are told to click.
        """
        async with verifying(db_sessionmaker, github_says()) as verification:
            pages = [
                await to_github(
                    verification,
                    await verification.link_for(
                        guild_id=GUILD, discord_user_id=ALICE, purpose=purpose
                    ),
                    member=ALICE,
                )
                for purpose in (
                    VerificationPurpose.LINK,
                    VerificationPurpose.REGISTER,
                    VerificationPurpose.UNREGISTER,
                )
            ]

        assert all("scope" not in page.url for page in pages), pages

    async def test_the_board_link_names_the_board_application(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A different registered application, because GitHub publishes no App permission for a
        user-owned board and granting an installed App an organisation one suspends its event
        delivery until an admin accepts."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            board = await to_github(
                verification,
                await verification.link_for(
                    guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.BOARD
                ),
                member=ALICE,
            )
            identity = await to_github(
                verification,
                await verification.link_for(
                    guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
                ),
                member=ALICE,
            )

        assert "client_id=Ov23liBoard" in board.url
        assert "client_id=Iv23liAbC" in identity.url

    async def test_the_code_is_exchanged_against_the_application_that_issued_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Which is why the state is consumed BEFORE the code is spent, and not only for the
        replay rule: the callback carries no hint of which application minted the code, so the
        purpose on the spent row is the only thing that can say.
        """
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/access_token"):
                asked.append(request.content.decode())
                return httpx.Response(
                    200, content=json.dumps({"access_token": "gho_abc", "scope": BOARD_SCOPE})
                )
            return httpx.Response(200, content=json.dumps({"login": "octocat", "id": 583231}))

        async with verifying(db_sessionmaker, handler) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)
            await followed(verification, purpose=VerificationPurpose.LINK)

        assert "client_id=Ov23liBoard" in asked[0]
        assert "client_id=Iv23liAbC" in asked[1]

    async def test_following_it_keeps_the_authorisation(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        held = await BoardCredentials(db_sessionmaker, keys=BOARD_KEY).granted_to(
            guild_id=GUILD, discord_user_id=ALICE
        )
        assert held is not None
        assert held.token == "gho_abc"
        assert held.github_login == "octocat"
        assert held.github_user_id == 583231

    async def test_the_stored_secret_is_not_the_token(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The point of the only encrypted column in the schema, asserted against the raw column
        rather than through the thing that decrypts it."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        kept = await db_session.scalar(select(BoardAuthorization.secret))
        assert kept is not None
        assert "gho_abc" not in kept, "the token is in the database in the clear"

    async def test_an_identity_link_keeps_no_authorisation(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The other arm, and the invariant the module docstring rests on: three of the four
        purposes still throw their token away."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        assert await db_session.scalar(select(BoardAuthorization.secret)) is None

    async def test_a_board_link_binds_no_github_account(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Authorising a board is not the same act as saying which account is yours, and the row
        `/link` writes is the one people are pinged through. Keeping them apart means authorising
        a board cannot quietly change who gets mentioned in a thread."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        assert await db_session.scalar(select(UserLink.github_username)) is None

    async def test_authorising_again_replaces_what_was_there(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """One row per person per server. Somebody who authorises a second account meant the
        second one, and two rows would leave the board reading under whichever was found first."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)
        async with verifying(
            db_sessionmaker, github_says(login="someone-else", user_id=42)
        ) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        logins = (await db_session.scalars(select(BoardAuthorization.github_login))).all()
        assert list(logins) == ["someone-else"]

    async def test_two_people_in_one_server_are_two_authorisations(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Which is what makes a card move attributable: the person who ran the command has their
        own, rather than borrowing whoever linked the board."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD, discord_user_id=ALICE)
        async with verifying(
            db_sessionmaker, github_says(login="hubot", user_id=42)
        ) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD, discord_user_id=BOB)

        kept = (await db_session.scalars(select(BoardAuthorization.discord_user_id))).all()
        assert sorted(kept) == [ALICE, BOB]

    async def test_a_deployment_with_no_key_says_the_trip_was_wasted(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The failure that can only happen to this purpose. Everybody else's token is finished
        with by the time the callback answers; a board's IS the thing being granted, so a
        deployment that cannot keep it has sent somebody to GitHub for nothing - and left a real
        authorisation standing on their account with nothing here using it. Saying "done" would be
        a lie they could not check.

        Late rather than never, since issue #201: `can_authorise_a_board` says no up front where
        there is no key, so what reaches this is a key that went away between the link being
        handed out and being followed.
        """
        async with verifying(db_sessionmaker, github_says(), keys="") as verification:
            link = await verification.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.BOARD
            )
            with pytest.raises(VerificationError, match="could not keep"):
                await round_trip(verification, link, member=ALICE)

        assert await db_session.scalar(select(BoardAuthorization.secret)) is None

    async def test_a_row_written_under_another_key_reads_as_absent(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A key rotated away, a row from another deployment, a corrupted column - none of them is
        a credential, and handing one back would mean sending GitHub a bearer token it never
        issued. The person authorises again and the row is replaced.
        """
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        stranger = BoardCredentials(db_sessionmaker, keys=OTHER_BOARD_KEY)
        assert await stranger.granted_to(guild_id=GUILD, discord_user_id=ALICE) is None

    async def test_a_rotated_key_still_reads_what_the_old_one_wrote(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Newest first, and both usable. Without this, rotating a key would mean every person who
        linked a board doing it again."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        rotated = BoardCredentials(db_sessionmaker, keys=f"{OTHER_BOARD_KEY},{BOARD_KEY}")
        held = await rotated.granted_to(guild_id=GUILD, discord_user_id=ALICE)
        assert held is not None
        assert held.token == "gho_abc"

    async def test_forgetting_it_leaves_nothing_behind(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """What a server unlinking its board does to the credential. Deleting this copy is not a
        revocation on GitHub's side, which is why the reply that reports it says so."""
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        assert await credentials.forget(guild_id=GUILD, discord_user_id=ALICE) is True
        assert await credentials.granted_to(guild_id=GUILD, discord_user_id=ALICE) is None
        assert await credentials.forget(guild_id=GUILD, discord_user_id=ALICE) is False

    async def test_an_authorisation_is_scoped_to_one_server(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The cross-tenant rule, at the storage end. A credential granted in one server is not a
        credential in another, however much the same person holds both."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        assert await credentials.granted_to(guild_id=GUILD + 1, discord_user_id=ALICE) is None

    async def test_a_deployment_is_told_about_the_two_applications_separately(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two registrations, either of which can be missing on its own.

        `configured` is still about the App alone, deliberately. The commands read
        `can_prove_identity` since #201's review; what still reads this is the enforcement of
        SHANNON_REQUIRE_PROVED_LINKS, which neither a missing board OAuth App nor a missing
        Discord half may switch off.
        """
        async with verifying(db_sessionmaker, github_says()) as both:
            assert both.configured is True
            assert both.can_authorise_a_board is True

        async with httpx.AsyncClient(transport=httpx.MockTransport(github_says())) as http:
            app_only = GitHubIdentityVerification(
                db_sessionmaker,
                UserLinkingService(db_sessionmaker),
                BoardCredentials(db_sessionmaker, keys=BOARD_KEY),
                board_links=NoBoardLinks(),
                tiers=FakeTiers(),
                client_id="Iv23liAbC",
                client_secret="shh",
                oauth_url="https://github.com",
                public_base_url="https://shannon.example.com",
                http=http,
                discord=DISCORD_APP,
            )
            assert app_only.configured is True
            assert app_only.can_authorise_a_board is False, (
                "a deployment with no OAuth App would hand out a link to an application that "
                "does not exist"
            )

    async def test_a_deployment_without_discord_authorises_no_board(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Found reviewing #201. A board link goes through Discord like every other link, so
        without it there is no board link either, however completely the board's own application
        and key are set up."""
        async with verifying(db_sessionmaker, github_says(), discord=NO_APPLICATION) as missing:
            assert missing.can_authorise_a_board is False
            assert missing.can_prove_identity is False
            assert missing.configured is True

    async def test_a_deployment_that_cannot_keep_an_authorisation_does_not_offer_one(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Issue #201. The OAuth App being registered is not enough on its own: with no key the
        round trip runs perfectly well and then cannot store its answer, so the only honest place
        to say so is before anybody is sent to GitHub rather than after they come back."""
        async with verifying(db_sessionmaker, github_says(), keys="") as keyless:
            assert keyless.configured is True
            assert keyless.can_authorise_a_board is False, (
                "a deployment with no key would send somebody to grant something it then throws "
                "away"
            )

        async with verifying(db_sessionmaker, github_says()) as keyed:
            assert keyed.can_authorise_a_board is True

    async def test_the_token_reaches_no_log_line(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Across the whole round trip, at DEBUG. The one credential this project stores is the
        one thing that must never be recoverable from a log file."""
        with caplog.at_level("DEBUG"):
            async with verifying(db_sessionmaker, github_says()) as verification:
                await followed(verification, purpose=VerificationPurpose.BOARD)

        assert "gho_abc" not in caplog.text


class TestTheBoardALinkIsFor:
    """Issue #201. One link both authorises and links, so the board somebody chose rides on the
    row the browser lands on - and only there."""

    async def test_it_rides_on_the_row_and_never_in_the_url(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The state stays the only thing in the link. A board in the URL would be one the
        browser could change on its way through Discord and GitHub and back; the row is the one
        place nothing outside this process can touch."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            plain = await verification.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.BOARD
            )
            chosen = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=6, owner="acme"),
            )

            # And where the link goes next, which since #201's review is GitHub only after
            # Discord: no board there either.
            plain_at = await to_github(verification, plain, member=ALICE)
            chosen_at = await to_github(verification, chosen, member=ALICE)

        # Found by everything after `state=`, so nothing else can be riding on the end of it.
        row = await db_session.scalar(
            select(IdentityVerification).where(IdentityVerification.state == state_of(chosen))
        )
        assert row is not None
        assert (row.board_number, row.board_owner) == (6, "acme")
        # And byte for byte the link it would have been without one, up to the state, which is
        # random in both - the link, and the GitHub page it leads to.
        assert chosen.partition("state=")[0] == plain.partition("state=")[0]
        assert chosen_at.url.partition("state=")[0] == plain_at.url.partition("state=")[0]


class TestWhatGitHubActuallyGranted:
    """Issue #201. Asked for is not granted.

    GitHub documents that a person can grant an application less than it asked for, and answers
    with a token all the same, with what was really granted in `scope`. A board kept on less would
    be found out later, as a board that will not open or a card that will not move, and a long way
    from the one person who could fix it. So the sign-in refuses there and then, and keeps nothing.
    """

    async def refused(
        self, sessionmaker: async_sessionmaker[AsyncSession], scope: object, *, code: str = "abc"
    ) -> None:
        """A board round trip GitHub answers with `scope`, which this expects to be turned away."""
        async with verifying(sessionmaker, github_says(scope=scope)) as verification:
            link = await verification.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.BOARD
            )
            with pytest.raises(VerificationError, match="granted less access"):
                await round_trip(verification, link, member=ALICE, code=code)

    @pytest.mark.parametrize("scope", ["", "read:project"])
    async def test_a_board_granted_less_than_it_asked_for_keeps_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        scope: str,
    ) -> None:
        """Nothing at all, and read without write - the second is the one somebody might choose
        on purpose, and it would leave every card move refusing."""
        await self.refused(db_sessionmaker, scope)

        assert await db_session.scalar(select(BoardAuthorization.secret)) is None
        # Not even a proof. The refusal comes before GitHub is asked who signed in, so the row
        # that answer would have written is not there either.
        assert await db_session.scalar(select(VerifiedIdentity.github_login)) is None

    @pytest.mark.parametrize("scope", [None, ["project"]])
    async def test_a_scope_that_is_not_text_counts_as_nothing_granted(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        scope: object,
    ) -> None:
        """Null reads the same as a response with no `scope` at all, and the right word in the
        wrong shape is still not a grant: guessing at what GitHub meant is how a short grant would
        be kept."""
        await self.refused(db_sessionmaker, scope)

        assert await db_session.scalar(select(BoardAuthorization.secret)) is None

    @pytest.mark.parametrize("scope", ["project", "project,read:org", " project "])
    async def test_a_board_granted_what_it_asked_for_is_kept(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], scope: str
    ) -> None:
        """Alongside something else too, since `scope` lists everything the token carries and that
        can be more than this asked for; and with stray space round it, which costs nothing to
        forgive."""
        async with verifying(db_sessionmaker, github_says(scope=scope)) as verification:
            await followed(verification, purpose=VerificationPurpose.BOARD)

        held = await BoardCredentials(db_sessionmaker, keys=BOARD_KEY).granted_to(
            guild_id=GUILD, discord_user_id=ALICE
        )
        assert held is not None
        assert held.token == "gho_abc"

    @pytest.mark.parametrize(
        "purpose",
        [VerificationPurpose.LINK, VerificationPurpose.REGISTER, VerificationPurpose.UNREGISTER],
    )
    async def test_an_identity_granted_no_scope_is_still_signed_in(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], purpose: VerificationPurpose
    ) -> None:
        """What a GitHub App's user token answers with, because identity asked for nothing - and
        nothing asked for is nothing missing, which is why the check has no purpose to look at."""
        async with verifying(db_sessionmaker, github_says(scope="")) as verification:
            await followed(verification, purpose=purpose)
            proved = await verification.ever_proved(guild_id=GUILD, discord_user_id=ALICE)

        assert proved is not None
        assert (proved.login, proved.github_user_id) == ("octocat", 583231)

    async def test_a_refusal_names_what_was_missing_and_no_credential(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Which scope was short is what an operator needs. The token and the code are both
        credentials, and a log file is the one place either could come to rest."""
        with caplog.at_level("DEBUG"):
            await self.refused(db_sessionmaker, "read:project", code="the-code")

        assert "without project" in caplog.text
        assert "gho_abc" not in caplog.text
        assert "the-code" not in caplog.text


class TestWhoseAuthorisationABoardIsReadUnder:
    """Issue #170. `reading` is the question the poller asks of every board, every couple of
    seconds, and getting it wrong is the one failure in this change that would be worse than the
    thing it replaced: a credential used across a tenancy boundary.
    """

    async def linked(
        self,
        session: AsyncSession,
        *,
        guild_id: int,
        number: int,
        owner: str | None,
        linked_by: int | None,
        repo_name: str = "Canon-Regularis/Shannon-bot",
        github_repo_id: int = 1,
        channel_id: int = 100,
    ) -> None:
        """A server with a board linked to it, and a member recorded as having authorised it."""
        await register_repository(
            session,
            guild_id=guild_id,
            channel_id=channel_id,
            github_repo_id=github_repo_id,
            repo_name=repo_name,
        )
        repository = await RepositoryStore(session).get_by_guild(guild_id)
        assert repository is not None
        await RepositoryStore(session).set_board(
            repository, project_number=number, project_owner=owner, linked_by=linked_by
        )
        await session.commit()

    async def test_a_board_reads_under_the_member_who_linked_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await self.linked(db_session, guild_id=1, number=6, owner="acme", linked_by=ALICE)
        await credentials.remember(
            guild_id=1,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=1,
            token="gho_alice",
        )

        assert await credentials.reading("acme", 6) == "gho_alice"

    async def test_a_board_whose_owner_is_the_repositorys_own_is_found_too(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """`project_owner` is null where the board belongs to the repository's own account, which
        is what most rows say. Resolved in the query, because the caller addresses the board by
        the owner it resolved rather than by the null - see `RepositoryStore.mirroring`."""
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await self.linked(db_session, guild_id=1, number=6, owner=None, linked_by=ALICE)
        await credentials.remember(
            guild_id=1,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=1,
            token="gho_alice",
        )

        assert await credentials.reading("Canon-Regularis", 6) == "gho_alice"

    async def test_two_servers_on_two_boards_of_one_account_do_not_share_a_credential(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """THE test for this change, and the bug an earlier design had.

        `mirroring` refuses two repositories on the SAME board, but two servers may link two
        DIFFERENT boards both owned by the same account. Resolved by owner, one server's board
        would be read under the other server's member's grant - a user credential crossing a
        tenancy boundary, which is exactly what this replaced.
        """
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await self.linked(db_session, guild_id=1, number=6, owner="acme", linked_by=ALICE)
        await self.linked(
            db_session,
            guild_id=2,
            number=77,
            owner="acme",
            linked_by=BOB,
            repo_name="acme/other",
            github_repo_id=2,
            channel_id=200,
        )
        for who, token in ((ALICE, "gho_alice"), (BOB, "gho_bob")):
            await credentials.remember(
                guild_id=1 if who is ALICE else 2,
                discord_user_id=who,
                github_login="octocat",
                github_user_id=1,
                token=token,
            )

        assert await credentials.reading("acme", 6) == "gho_alice"
        assert await credentials.reading("acme", 77) == "gho_bob"

    async def test_a_board_nobody_linked_reads_as_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Which is every board that existed before this shipped: the column is null, and null
        means nobody has authorised one. An empty answer sends the read out anonymous, GitHub
        answers 404 or 403, and that lands in the pair every unreadable-board site already
        catches - so "nobody authorised this" needed no new branch anywhere."""
        await self.linked(db_session, guild_id=1, number=6, owner="acme", linked_by=None)

        assert await BoardCredentials(db_sessionmaker, keys=BOARD_KEY).reading("acme", 6) == ""

    async def test_a_board_no_server_linked_reads_as_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A board nothing points at, which is what a stale cache or a wrong number looks like."""
        assert await BoardCredentials(db_sessionmaker, keys=BOARD_KEY).reading("acme", 6) == ""

    async def test_a_linker_who_withdrew_reads_as_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The row names them and their authorisation is gone, which is what withdrawing does."""
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await self.linked(db_session, guild_id=1, number=6, owner="acme", linked_by=ALICE)

        assert await credentials.reading("acme", 6) == ""


class TestWhoseAuthorisationMovesACard:
    """`moving` is the other half, and the two are deliberately different questions: a board's own
    reads belong to the board, where a write belongs to whoever asked for it."""

    async def test_it_answers_the_members_own(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await credentials.remember(
            guild_id=1,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=1,
            token="gho_alice",
        )

        assert await credentials.moving(guild_id=1, discord_user_id=ALICE) == "gho_alice"

    async def test_somebody_who_authorised_nothing_gets_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Deliberately not a fallback to whoever linked the board. Moving a card as somebody else
        is the thing this change exists to stop, so an empty answer becomes a refusal."""
        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        await credentials.remember(
            guild_id=1,
            discord_user_id=ALICE,
            github_login="octocat",
            github_user_id=1,
            token="gho_alice",
        )

        assert await credentials.moving(guild_id=1, discord_user_id=BOB) == ""
