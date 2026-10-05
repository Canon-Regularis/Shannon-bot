"""A link signs in only the member it was issued for, in the browser that proved it.

Found reviewing #201. A one-time link used to point straight at GitHub, and its callback trusted
nothing but the state in its query string. GitHub skips its consent page for an application
somebody has already authorised, so a link forwarded to such a person was finished by them,
silently, and recorded as its ISSUER: a name under `/link`, the evidence `/register` and
`/unregister` act on, and for a board a `project` token that acts as whoever clicked.

Driven against a real database and the real service, with Discord and GitHub as functions on one
transport that records everything sent to either. Nearly every test here is somebody trying to
finish a link that is not theirs, or to finish theirs somewhere it did not begin, and what is
asserted is that nothing was recorded - and, wherever it can be, that nobody was even asked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import BoardAuthorization, IdentityVerification, UserLink, VerifiedIdentity
from shannon.db.stores.identities import IdentityVerificationStore, PendingLink
from shannon.domain.enums import VerificationPurpose
from shannon.services.board_credentials import BoardCredentials
from shannon.services.linking import UserLinkingService
from shannon.services.verification import (
    BOARD_SCOPE,
    LINK_LIFETIME,
    GitHubIdentityVerification,
    OAuthClient,
    VerificationError,
    _binding,
    _sealed,
)
from tests.fakes.board_links import NoBoardLinks
from tests.support.credentials import BOARD_KEY
from tests.support.round_trip import (
    BROWSER,
    DISCORD_APP,
    OTHER_BROWSER,
    browser_on,
    discord_says,
    round_trip,
    signs_in,
    state_of,
    to_discord,
    to_github,
)

pytestmark = pytest.mark.integration

GUILD = 1
# Alice runs the command. Bob is who she sends the link to.
ALICE = 555
BOB = 777

FORWARDED = "made for somebody else"
UNFOLLOWABLE = "expired or has already been used"
ELSEWHERE = "has to finish in the browser it began in"

# Shaped like states this bot mints, so a refusal of either is the database's answer rather than
# the shape check's.
NEVER_ISSUED = "n" * 43
FROM_BEFORE = "f" * 43

Handler = Callable[[httpx.Request], httpx.Response]


def github(request: httpx.Request) -> httpx.Response:
    """GitHub, finishing any round trip as one account and granting a board what it asks for."""
    if request.url.path.endswith("/access_token"):
        return httpx.Response(200, json={"access_token": "gho_granted", "scope": BOARD_SCOPE})
    return httpx.Response(200, json={"login": "octocat", "id": 583231})


class Wire:
    """Everything that left the process, Discord's requests and GitHub's alike."""

    def __init__(self) -> None:
        self.asked: list[httpx.Request] = []

    def to(self, host: str) -> list[httpx.Request]:
        return [request for request in self.asked if request.url.host == host]

    def to_github(self) -> list[httpx.Request]:
        return [request for request in self.asked if request.url.host.endswith("github.com")]


@asynccontextmanager
async def verifying(
    sessionmaker: async_sessionmaker[AsyncSession], *, discord: Handler | None = None
) -> AsyncIterator[tuple[GitHubIdentityVerification, Wire]]:
    """The real service, and the wire it talks over."""
    wire = Wire()
    answer = discord or discord_says(github)

    def recorded(request: httpx.Request) -> httpx.Response:
        wire.asked.append(request)
        return answer(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorded)) as http:
        yield service(sessionmaker, http), wire


def service(
    sessionmaker: async_sessionmaker[AsyncSession], http: httpx.AsyncClient
) -> GitHubIdentityVerification:
    return GitHubIdentityVerification(
        sessionmaker,
        UserLinkingService(sessionmaker),
        BoardCredentials(sessionmaker, keys=BOARD_KEY),
        board_links=NoBoardLinks(),
        client_id="Iv23liAbC",
        client_secret="shh",
        oauth_url="https://github.com",
        public_base_url="https://shannon.example.com",
        http=http,
        board=OAuthClient(client_id="Ov23liBoard", client_secret="board-shh", scope=BOARD_SCOPE),
        discord=DISCORD_APP,
    )


async def issued(
    verification: GitHubIdentityVerification,
    *,
    purpose: VerificationPurpose = VerificationPurpose.BOARD,
) -> str:
    """A link handed out to Alice: the one she is shown, and could forward."""
    return await verification.link_for(guild_id=GUILD, discord_user_id=ALICE, purpose=purpose)


async def row_for(session: AsyncSession, link: str) -> IdentityVerification:
    session.expire_all()
    found = await session.scalar(
        select(IdentityVerification).where(IdentityVerification.state == state_of(link))
    )
    assert found is not None
    return found


async def nothing_recorded(session: AsyncSession) -> None:
    """No proof, no account and no authorisation was written for anybody."""
    session.expire_all()
    assert (await session.scalars(select(VerifiedIdentity))).all() == []
    assert (await session.scalars(select(UserLink))).all() == []
    assert (await session.scalars(select(BoardAuthorization))).all() == []


class TestAForwardedLink:
    """Alice runs the command and sends Bob the link. Bob has authorised this bot's GitHub
    application before, so GitHub would have shown him nothing at all."""

    @pytest.mark.parametrize("purpose", list(VerificationPurpose))
    async def test_discord_names_him_and_it_goes_no_further(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        purpose: VerificationPurpose,
    ) -> None:
        """Every purpose, because every purpose shared the hole."""
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification, purpose=purpose)
            with pytest.raises(VerificationError, match=FORWARDED):
                await to_github(verification, link, member=BOB, browser=OTHER_BROWSER)

        assert wire.to_github() == [], "GitHub was asked about a link Discord had refused"
        await nothing_recorded(db_session)

    async def test_through_the_routes_it_is_a_page_saying_so(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            async with browser_on(verification) as bobs:
                opened = await bobs.get("/oauth/start", params={"state": state_of(link)})
                back = await bobs.get(
                    "/oauth/discord/callback",
                    params={"code": signs_in(BOB), "state": state_of(opened.headers["location"])},
                )

        assert back.status_code == 400
        assert FORWARDED in back.text
        assert wire.to_github() == []
        await nothing_recorded(db_session)

    async def test_it_is_still_alices_afterwards(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """A refusal spends nothing and binds nothing, so whoever was sent the link cannot use it
        up either: the member it was issued for can still finish it."""
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)
            with pytest.raises(VerificationError, match=FORWARDED):
                await to_github(verification, link, member=BOB, browser=OTHER_BROWSER)
            row = await row_for(db_session, link)
            assert (row.bound_browser, row.consumed_at) == (None, None)

            verified = await round_trip(verification, link, member=ALICE)

        assert verified.discord_user_id == ALICE

    async def test_the_log_names_the_issuer_and_not_whoever_followed_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Whoever was sent somebody else's link was very likely sent it on purpose, by somebody
        else, and writing their Discord id down would keep a fact about them they never gave."""
        with caplog.at_level("WARNING", logger="shannon.services.verification"):
            async with verifying(db_sessionmaker) as (verification, _):
                link = await issued(verification)
                with pytest.raises(VerificationError, match=FORWARDED):
                    await to_github(verification, link, member=BOB, browser=OTHER_BROWSER)

        assert f"discord:{ALICE} in guild {GUILD}" in caplog.text
        assert f"discord:{BOB}" not in caplog.text
        assert str(BOB) not in caplog.messages[-1]

    async def test_skipping_discord_gets_nowhere(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Bob seals the state himself - anybody can, for a cookie they hold - and goes straight
        to the GitHub callback with a code of his own. The row was never bound, so it matches no
        browser at all, and GitHub is never asked to spend his code."""
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(
                    state=_sealed(OTHER_BROWSER, "github", state_of(link)),
                    code="bobs-code",
                    browser=OTHER_BROWSER,
                )

        assert wire.asked == []
        await nothing_recorded(db_session)


class TestTheGitHubPageForwarded:
    """Alice goes through Discord herself and forwards the page her browser was sent to next. That
    URL is exactly what the old link was, so this is the old attack, one step later."""

    async def test_it_is_refused_in_any_other_browser_before_github_is_asked(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            at = await to_github(verification, await issued(verification), member=ALICE)
            asked = len(wire.asked)
            # Bob opens it; GitHub sends his browser back with his code and her sealed state.
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(state=at.state, code="bobs-code", browser=OTHER_BROWSER)

            assert wire.asked[asked:] == [], "somebody else's code was spent"
            await nothing_recorded(db_session)

            # And it is still hers to finish, in the browser that proved itself.
            verified = await verification.redeem(state=at.state, code="abc", browser=at.browser)

        assert verified.discord_user_id == ALICE

    async def test_resealing_it_for_another_browser_gets_nowhere(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """A seal Bob makes for his own cookie opens - and the row is bound to Alice's browser,
        so it still spends nothing."""
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            await to_github(verification, link, member=ALICE)
            asked = len(wire.asked)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(
                    state=_sealed(OTHER_BROWSER, "github", state_of(link)),
                    code="bobs-code",
                    browser=OTHER_BROWSER,
                )

        assert wire.asked[asked:] == []
        await nothing_recorded(db_session)

    @pytest.mark.parametrize("cookie", ["", "not-one-of-ours"])
    async def test_a_browser_with_no_cookie_of_ours_is_refused_before_anything(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], cookie: str
    ) -> None:
        """A missing cookie would be an empty key, and anybody can compute an HMAC under that."""
        async with verifying(db_sessionmaker) as (verification, wire):
            at = await to_github(verification, await issued(verification), member=ALICE)
            asked = len(wire.asked)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(state=at.state, code="abc", browser=cookie)

        assert wire.asked[asked:] == []


class TestSomebodyElsesRoundTripPushedIntoYourBrowser:
    """Login CSRF. Somebody starts a round trip of their own, holds back the return, and makes
    another person's browser finish it - so that person's account lands on their row."""

    async def test_a_discord_return_from_another_browser_is_refused_before_discord_is_asked(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            discord = await to_discord(verification, link)

            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(
                    state=state_of(discord), code=signs_in(ALICE), browser=OTHER_BROWSER
                )

        assert wire.asked == []
        assert (await row_for(db_session, link)).bound_browser is None

    async def test_a_discord_return_with_no_cookie_is_refused_the_same_way(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            discord = await to_discord(verification, await issued(verification))

            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(
                    state=state_of(discord), code=signs_in(ALICE), browser=""
                )

        assert wire.asked == []

    @pytest.mark.parametrize("state", ["", "no-seal-at-all", ".only-a-seal", "S.forged"])
    async def test_a_discord_return_with_no_seal_of_ours_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], state: str
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(
                    state=state, code=signs_in(ALICE), browser=BROWSER
                )

        assert wire.asked == []

    async def test_no_cookie_is_never_a_key(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """A browser with no cookie would be an empty HMAC key, and anybody can seal under that.
        So a return sealed with one - computed here the way anybody could - is refused before
        Discord is asked, rather than binding the link to every browser that has no cookie."""
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            state = state_of(link)
            mac = hmac.new(b"", f"discord:{state}".encode(), hashlib.sha256).digest()
            forged = f"{state}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"

            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(state=forged, code=signs_in(ALICE), browser="")

        assert wire.asked == []
        assert (await row_for(db_session, link)).bound_browser is None

    async def test_a_seal_for_one_leg_opens_nothing_on_the_other(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """One seal per leg, so neither can be replayed at the other's door."""
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)
            discord = await to_discord(verification, link)
            at = await to_github(verification, link, member=ALICE)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(state=state_of(discord), code="abc", browser=BROWSER)
            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(
                    state=at.state, code=signs_in(ALICE), browser=BROWSER
                )

    async def test_a_seal_with_characters_no_seal_has_is_refused_rather_than_raising(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Compared as bytes, because `compare_digest` raises on a `str` that is not ASCII and the
        state arrives from a query string anybody can write."""
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(state=f"{state_of(link)}.é", code="abc", browser=BROWSER)


class TestOneMemberAndTheirBrowsers:
    async def test_two_links_in_one_browser_each_finish(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, _):
            first = await to_github(verification, await issued(verification), member=ALICE)
            second = await to_github(verification, await issued(verification), member=ALICE)

            for at in (first, second):
                await verification.redeem(state=at.state, code="abc", browser=at.browser)

    async def test_proving_it_again_in_another_browser_moves_it_there(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Alice opens her link on her laptop, then again on her phone. Only she can bind it, so
        the last browser she proved herself in is the one that may finish."""
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)
            laptop = await to_github(verification, link, member=ALICE, browser=BROWSER)
            phone = await to_github(verification, link, member=ALICE, browser=OTHER_BROWSER)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(state=laptop.state, code="abc", browser=laptop.browser)
            verified = await verification.redeem(
                state=phone.state, code="abc", browser=phone.browser
            )

        assert verified.discord_user_id == ALICE


class TestALinkFromBeforeThis:
    async def test_one_handed_out_before_the_upgrade_cannot_be_finished(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """It pointed straight at GitHub, so its callback brings the bare state back, in a browser
        that has never been here. Nothing bound it and nothing ever will, so it is told the link
        has expired - and costs the person ten minutes at most."""
        await IdentityVerificationStore(db_session).issue(
            state=FROM_BEFORE,
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.LINK,
            lifetime=LINK_LIFETIME,
        )
        await db_session.commit()

        async with verifying(db_sessionmaker) as (verification, wire):
            for state, browser in (
                (FROM_BEFORE, ""),
                (FROM_BEFORE, BROWSER),
                (_sealed(BROWSER, "github", FROM_BEFORE), BROWSER),
            ):
                with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                    await verification.redeem(state=state, code="abc", browser=browser)

        assert wire.asked == []
        await nothing_recorded(db_session)


class TestAStateNoLinkCouldHave:
    """Found reviewing #201's fix. Postgres answers a NUL byte in a text parameter with an error
    rather than with no row, so a NUL reaching a query was a 500 and a traceback, from a request to
    `/oauth/start` that needed no cookie, no link and no seal. Only a state shaped like one this
    bot mints is ever looked up now - at every stop, since anybody can seal anything for a cookie
    of their own."""

    @pytest.mark.parametrize(
        "state",
        ["\x00", "a" * 42 + "\x00", "s" * 42, "s" * 44, chr(0xE9) * 43, "a" * 42 + "."],
        ids=["nul", "a-trailing-nul", "too-short", "too-long", "not-ascii", "a-dot"],
    )
    async def test_it_is_refused_at_every_stop_before_anything_is_asked(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], state: str
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.start(state=state, browser=BROWSER)
            with pytest.raises(VerificationError, match=ELSEWHERE):
                await verification.prove_on_discord(
                    state=_sealed(BROWSER, "discord", state), code=signs_in(ALICE), browser=BROWSER
                )
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.redeem(
                    state=_sealed(BROWSER, "github", state), code="abc", browser=BROWSER
                )

        assert wire.asked == []

    async def test_a_nul_through_the_route_is_a_page_rather_than_a_server_error(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with (
            verifying(db_sessionmaker) as (verification, _),
            browser_on(verification) as client,
        ):
            response = await client.get("/oauth/start", params={"state": "\x00"})

        assert response.status_code == 400
        assert UNFOLLOWABLE in response.text


class TestOpeningALink:
    async def test_it_writes_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """A link preview, a crawler or somebody opening it twice all land here, and none of them
        may spend or bind it."""
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            for browser in (BROWSER, BROWSER, OTHER_BROWSER):
                await to_discord(verification, link, browser=browser)

        row = await row_for(db_session, link)
        assert (row.bound_browser, row.consumed_at) == (None, None)
        assert wire.asked == []
        pending = await IdentityVerificationStore(db_session).pending(state_of(link))
        assert pending == PendingLink(GUILD, ALICE, VerificationPurpose.BOARD)

    @pytest.mark.parametrize("browser", ["", "short", "c" * 44])
    async def test_it_will_not_seal_for_a_browser_that_is_not_one_of_ours(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], browser: str
    ) -> None:
        """The route always mints a well-formed one, so this is the service declining to trust
        that it did."""
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)

            with pytest.raises(VerificationError, match=ELSEWHERE):
                await to_discord(verification, link, browser=browser)

    async def test_a_link_nobody_issued_goes_nowhere(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, _):
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.start(state=NEVER_ISSUED, browser=BROWSER)

    async def test_a_spent_link_goes_nowhere_at_any_stop(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            link = await issued(verification)
            await round_trip(verification, link, member=ALICE)
            asked = len(wire.asked)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await to_discord(verification, link)
            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await verification.prove_on_discord(
                    state=_sealed(BROWSER, "discord", state_of(link)),
                    code=signs_in(ALICE),
                    browser=BROWSER,
                )

        assert wire.asked[asked:] == [], "Discord was asked about a link already spent"

    async def test_a_link_spent_while_this_browser_was_at_discord_is_not_bound(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Alice opened her link in two browsers and finished it in the other one while this one
        was at Discord. Binding is guarded on the link still being followable, so the late return
        binds nothing and goes no further."""
        answer = discord_says(github)

        async def spent_meanwhile(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/users/@me"):
                async with db_sessionmaker() as session, session.begin():
                    await session.execute(
                        update(IdentityVerification).values(consumed_at=func.now())
                    )
            return answer(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(spent_meanwhile)) as http:
            verification = service(db_sessionmaker, http)
            link = await issued(verification)

            with pytest.raises(VerificationError, match=UNFOLLOWABLE):
                await to_github(verification, link, member=ALICE)

        assert (await row_for(db_session, link)).bound_browser is None

    async def test_the_row_holds_a_keyed_hash_and_never_the_cookie(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Nothing in the database could be replayed: not the cookie, and not a hash two rows
        would share and so give away as one browser."""
        async with verifying(db_sessionmaker) as (verification, _):
            one = await issued(verification)
            two = await issued(verification)
            for link in (one, two):
                await to_github(verification, link, member=ALICE)

        # Read out one at a time: `row_for` expires the session, the first row with it.
        first = (await row_for(db_session, one)).bound_browser
        second = (await row_for(db_session, two)).bound_browser
        assert first == _binding(BROWSER, state_of(one))
        assert first != BROWSER
        assert first != second


class TestWhatDiscordIsAskedAndHow:
    async def test_the_authorize_page_asks_for_who_you_are_and_nothing_more(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, _):
            link = await issued(verification)
            page = await to_discord(verification, link)

        url = urlsplit(page)
        asked = parse_qs(url.query)
        sealed = asked.pop("state")[0]
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://discord.com/oauth2/authorize"
        assert asked == {
            "client_id": [DISCORD_APP.client_id],
            "redirect_uri": ["https://shannon.example.com/oauth/discord/callback"],
            "response_type": ["code"],
            "scope": ["identify"],
            "prompt": ["none"],
        }
        assert sealed.startswith(f"{state_of(link)}.")
        assert page.endswith(f"&state={sealed}"), "the state is no longer last"

    async def test_the_code_is_exchanged_with_the_secret_in_basic_auth_and_never_in_the_body(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker) as (verification, wire):
            await to_github(verification, await issued(verification), member=ALICE)

        exchange, whoami = wire.to("discord.com")
        expected = f"{DISCORD_APP.client_id}:{DISCORD_APP.client_secret}".encode()
        assert (exchange.method, str(exchange.url)) == (
            "POST",
            "https://discord.com/api/v10/oauth2/token",
        )
        assert exchange.headers["authorization"] == f"Basic {base64.b64encode(expected).decode()}"
        assert parse_qs(exchange.content.decode()) == {
            "grant_type": ["authorization_code"],
            "code": [signs_in(ALICE)],
            "redirect_uri": ["https://shannon.example.com/oauth/discord/callback"],
        }
        assert DISCORD_APP.client_secret not in exchange.content.decode()
        assert (whoami.method, str(whoami.url)) == ("GET", "https://discord.com/api/v10/users/@me")
        assert whoami.headers["authorization"] == f"Bearer discord-{ALICE}"


def a_discord_that(
    *,
    token: httpx.Response | None = None,
    me: httpx.Response | None = None,
    fails: Exception | None = None,
) -> Handler:
    """Discord answering its two calls as told, and GitHub as usual."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "discord.com":
            return github(request)
        if fails is not None:
            raise fails
        if request.url.path.endswith("/oauth2/token"):
            if token is not None:
                return token
            return httpx.Response(200, json={"access_token": f"discord-{ALICE}"})
        return me if me is not None else httpx.Response(200, json={"id": str(ALICE)})

    return handler


REFUSED = "Discord would not complete the sign-in"
SILENT = "Discord would not say who signed in"


class TestWhenDiscordWillNotSay:
    """Each of them binds nothing and asks GitHub nothing: the person runs the command again."""

    @pytest.mark.parametrize(
        ("discord", "said"),
        [
            (a_discord_that(token=httpx.Response(400, json={"error": "invalid_grant"})), REFUSED),
            (a_discord_that(token=httpx.Response(200, json={"token_type": "Bearer"})), REFUSED),
            (a_discord_that(token=httpx.Response(200, json={"access_token": ""})), REFUSED),
            (a_discord_that(token=httpx.Response(502, text="<html>bad gateway</html>")), REFUSED),
            (a_discord_that(fails=httpx.ConnectError("refused")), REFUSED),
            (a_discord_that(me=httpx.Response(200, json={"username": "alice"})), SILENT),
            (a_discord_that(me=httpx.Response(200, json={"id": ALICE})), SILENT),
            # Fullwidth digits, which `str.isdigit` accepts and `int` would read as 555.
            (a_discord_that(me=httpx.Response(200, json={"id": chr(0xFF15) * 3})), SILENT),
            (a_discord_that(me=httpx.Response(200, json={"id": f" {ALICE}"})), SILENT),
            (a_discord_that(me=httpx.Response(200, json={"id": ""})), SILENT),
        ],
        ids=[
            "a-refused-code",
            "no-token",
            "an-empty-token",
            "an-outage-page",
            "unreachable",
            "no-id",
            "an-id-that-is-a-number",
            "an-id-in-another-script",
            "an-id-with-whitespace",
            "an-empty-id",
        ],
    )
    async def test_nothing_is_bound(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        discord: Handler,
        said: str,
    ) -> None:
        async with verifying(db_sessionmaker, discord=discord) as (verification, wire):
            link = await issued(verification)
            with pytest.raises(VerificationError, match=said):
                await to_github(verification, link, member=ALICE)

        assert (await row_for(db_session, link)).bound_browser is None
        assert wire.to_github() == []

    async def test_discords_reason_is_logged_as_its_code_and_nothing_more(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        refusing = a_discord_that(
            token=httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Invalid code as:555."},
            )
        )

        with caplog.at_level("WARNING", logger="shannon.services.verification"):
            async with verifying(db_sessionmaker, discord=refusing) as (verification, _):
                with pytest.raises(VerificationError, match=REFUSED):
                    await to_github(verification, await issued(verification), member=ALICE)

        assert "invalid_grant" in caplog.text
        assert "Invalid code" not in caplog.text

    async def test_unreachable_is_logged_by_what_went_wrong(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        down = a_discord_that(fails=httpx.ConnectError("refused"))

        with caplog.at_level("WARNING", logger="shannon.services.verification"):
            async with verifying(db_sessionmaker, discord=down) as (verification, _):
                with pytest.raises(VerificationError, match=REFUSED):
                    await to_github(verification, await issued(verification), member=ALICE)

        assert "ConnectError" in caplog.text


class TestNothingSecretIsWrittenDown:
    async def test_a_round_trip_and_a_refusal_leave_no_credential_in_the_log(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        """At DEBUG, across everything: both applications' secrets, both tokens, both codes, the
        cookies, the state and what the row holds for the browser."""
        with caplog.at_level("DEBUG"):
            async with verifying(db_sessionmaker) as (verification, _):
                forwarded = await issued(verification)
                with pytest.raises(VerificationError, match=FORWARDED):
                    await to_github(verification, forwarded, member=BOB, browser=OTHER_BROWSER)
                link = await issued(verification)
                await round_trip(verification, link, member=ALICE, code="the-github-code")

        for secret in (
            DISCORD_APP.client_secret,
            "board-shh",
            f"discord-{ALICE}",
            f"discord-{BOB}",
            "gho_granted",
            "the-github-code",
            signs_in(ALICE),
            signs_in(BOB),
            BROWSER,
            OTHER_BROWSER,
            state_of(link),
            state_of(forwarded),
            _binding(BROWSER, state_of(link)),
        ):
            assert secret not in caplog.text, f"{secret} reached a log line"
