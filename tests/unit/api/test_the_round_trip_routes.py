"""The three stops a browser makes on the way through a one-time link, against a stand-in service.

Found reviewing #201. What is held here is what the ROUTES own: the cookie and every attribute that
makes it safe, what each stop hands the service, and what a browser is told when something is
missing. What the service decides with those - who may finish what - is held against a real
database in `tests/integration/test_a_link_is_bound_to_its_member.py`.
"""

from __future__ import annotations

import httpx
import pytest

from shannon.api.routes.oauth import (
    CANCELLED,
    INCOMPLETE,
    ROUND_TRIP_COOKIE,
    ROUND_TRIP_SECONDS,
)
from shannon.domain.enums import VerificationPurpose
from shannon.services.verification import (
    BROWSER_SHAPE,
    LINK_LIFETIME,
    VerificationError,
    Verified,
)
from tests.support.round_trip import BROWSER, browser_on

pytestmark = pytest.mark.unit

DISCORD = "https://discord.com/oauth2/authorize?state=sealed"
GITHUB = "https://github.com/login/oauth/authorize?state=sealed-again"

# A browser presenting the cookie a link left in it, written as the header it sends.
HOLDING = {"Cookie": f"{ROUND_TRIP_COOKIE}={BROWSER}"}

# A value somebody else chose and put in a browser.
PLANTED = "p" * 43


class FakeVerification:
    """What the routes ask of the service, and what each stop handed it."""

    def __init__(self, *, can: bool = True, refusal: str | None = None) -> None:
        self.can_sign_in_with_discord = can
        self.refusal = refusal
        self.started: list[tuple[str, str]] = []
        self.proved: list[tuple[str, str, str]] = []
        self.redeemed: list[tuple[str, str, str]] = []

    def refuse_if_told_to(self) -> None:
        if self.refusal is not None:
            raise VerificationError(self.refusal)

    async def start(self, *, state: str, browser: str) -> str:
        self.started.append((state, browser))
        self.refuse_if_told_to()
        return DISCORD

    async def prove_on_discord(self, *, state: str, code: str, browser: str) -> str:
        self.proved.append((state, code, browser))
        self.refuse_if_told_to()
        return GITHUB

    async def redeem(self, *, state: str, code: str, browser: str) -> Verified:
        self.redeemed.append((state, code, browser))
        self.refuse_if_told_to()
        return Verified(
            guild_id=1,
            discord_user_id=555,
            login="octocat",
            github_user_id=583231,
            purpose=VerificationPurpose.LINK,
        )


def attributes_of(response: httpx.Response) -> tuple[str, set[str]]:
    """The cookie a response set, as its value and its attributes, lowercased."""
    name_and_value, *attributes = response.headers["set-cookie"].split(";")
    name, _, value = name_and_value.partition("=")
    assert name == ROUND_TRIP_COOKIE
    return value, {attribute.strip().lower() for attribute in attributes}


class TestOpeningALink:
    async def test_it_sends_the_browser_to_discord_holding_a_fresh_cookie(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get("/oauth/start", params={"state": "S"})

        assert response.status_code == 303
        assert response.headers["location"] == DISCORD
        value, _ = attributes_of(response)
        assert BROWSER_SHAPE.fullmatch(value)
        assert verification.started == [("S", value)]

    async def test_the_cookie_is_held_to_this_browser_and_this_site(self) -> None:
        """`__Host-` is only honoured Secure, on Path=/ and with no Domain, which is what stops a
        sibling subdomain or plain http planting a value of its own. HttpOnly, so no script on any
        page can read it; Lax, so it still arrives on the top-level redirect back from Discord."""
        async with browser_on(FakeVerification()) as client:
            response = await client.get("/oauth/start", params={"state": "S"})

        _, attributes = attributes_of(response)
        assert ROUND_TRIP_COOKIE.startswith("__Host-")
        assert {"secure", "httponly", "path=/", "samesite=lax"} <= attributes
        assert f"max-age={ROUND_TRIP_SECONDS}" in attributes
        assert not any(attribute.startswith("domain") for attribute in attributes)

    async def test_it_lives_as_long_as_a_link_does(self) -> None:
        assert LINK_LIFETIME.total_seconds() == ROUND_TRIP_SECONDS

    async def test_nothing_on_the_way_is_cached(self) -> None:
        async with browser_on(FakeVerification()) as client:
            response = await client.get("/oauth/start", params={"state": "S"})

        assert response.headers["cache-control"] == "no-store"

    async def test_a_cookie_the_browser_already_holds_is_kept(self) -> None:
        """Two links followed in one browser, and the second must not knock the first over."""
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get("/oauth/start", params={"state": "S"}, headers=HOLDING)

        value, _ = attributes_of(response)
        assert value == BROWSER
        assert verification.started == [("S", BROWSER)]

    @pytest.mark.parametrize("planted", ["", "short", "x" * 44, "a" * 42 + "!"])
    async def test_a_cookie_that_is_not_one_of_ours_is_replaced(self, planted: str) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get(
                "/oauth/start",
                params={"state": "S"},
                headers={"Cookie": f"{ROUND_TRIP_COOKIE}={planted}"},
            )

        value, _ = attributes_of(response)
        assert value != planted
        assert BROWSER_SHAPE.fullmatch(value)

    async def test_every_browser_gets_its_own(self) -> None:
        async with browser_on(FakeVerification()) as one, browser_on(FakeVerification()) as two:
            first = await one.get("/oauth/start", params={"state": "S"})
            second = await two.get("/oauth/start", params={"state": "S"})

        assert attributes_of(first)[0] != attributes_of(second)[0]

    async def test_a_link_with_no_state_is_incomplete(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get("/oauth/start")

        assert response.status_code == 400
        assert response.json()["detail"] == INCOMPLETE
        assert verification.started == []

    async def test_a_refusal_is_a_page_and_leaves_no_cookie(self) -> None:
        verification = FakeVerification(refusal="That link has expired.")

        async with browser_on(verification) as client:
            response = await client.get("/oauth/start", params={"state": "S"})

        assert response.status_code == 400
        assert response.json()["detail"] == "That link has expired."
        assert "set-cookie" not in response.headers


class TestComingBackFromDiscord:
    async def test_it_sends_the_browser_on_to_github(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get(
                "/oauth/discord/callback",
                params={"code": "c", "state": "sealed"},
                headers=HOLDING,
            )

        assert response.status_code == 303
        assert response.headers["location"] == GITHUB
        assert response.headers["cache-control"] == "no-store"
        assert verification.proved == [("sealed", "c", BROWSER)]

    async def test_it_sets_no_cookie_of_its_own(self) -> None:
        """Only the first stop mints one. Minting here would let a browser that never opened the
        link start halfway through."""
        async with browser_on(FakeVerification()) as client:
            response = await client.get(
                "/oauth/discord/callback", params={"code": "c", "state": "sealed"}
            )

        assert "set-cookie" not in response.headers

    async def test_a_browser_with_no_cookie_is_handed_over_as_one(self) -> None:
        """The service refuses it, before any key is made from it; the shape is checked there."""
        verification = FakeVerification()

        async with browser_on(verification) as client:
            await client.get("/oauth/discord/callback", params={"code": "c", "state": "sealed"})

        assert verification.proved == [("sealed", "c", "")]

    async def test_cancelling_on_discord_says_so_and_asks_nothing(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get(
                "/oauth/discord/callback",
                params={"error": "access_denied", "state": "sealed"},
                headers=HOLDING,
            )

        assert response.status_code == 400
        assert response.json()["detail"] == CANCELLED
        assert verification.proved == []

    @pytest.mark.parametrize("params", [{}, {"code": "c"}, {"state": "sealed"}])
    async def test_an_incomplete_return_is_refused(self, params: dict[str, str]) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get("/oauth/discord/callback", params=params)

        assert response.status_code == 400
        assert response.json()["detail"] == INCOMPLETE
        assert verification.proved == []

    async def test_a_refusal_is_a_page(self) -> None:
        verification = FakeVerification(refusal="This link was made for somebody else.")

        async with browser_on(verification) as client:
            response = await client.get(
                "/oauth/discord/callback", params={"code": "c", "state": "sealed"}
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "This link was made for somebody else."


class TestFinishingAtGitHub:
    async def test_the_browser_is_handed_over_with_the_code_and_state(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            response = await client.get(
                "/oauth/github/callback",
                params={"code": "c", "state": "sealed-again"},
                headers=HOLDING,
            )

        assert response.status_code == 200
        assert response.text.startswith("Signed in as octocat.")
        assert verification.redeemed == [("sealed-again", "c", BROWSER)]

    async def test_a_browser_with_no_cookie_is_handed_over_as_one(self) -> None:
        verification = FakeVerification()

        async with browser_on(verification) as client:
            await client.get("/oauth/github/callback", params={"code": "c", "state": "s"})

        assert verification.redeemed == [("s", "c", "")]


async def asked(
    verification: FakeVerification,
    path: str,
    params: dict[str, str],
    headers: httpx.Headers,
) -> httpx.Response:
    """One request, from a browser holding exactly the cookies in `headers` and no others."""
    async with browser_on(verification) as client:
        return await client.get(path, params=params, headers=headers)


class TestReadingTheCookie:
    """Found reviewing #201's fix. Starlette strips every cookie name with `str.strip()`, which
    removes a leading no-break space or NEL as well as a blank - so a cookie held under this name
    behind one of those, which no `__Host-` rule ever applied to, was read as the round-trip
    cookie, and `/oauth/start` handed it back as a genuine one. The routes read it themselves now,
    by its exact name."""

    @pytest.mark.parametrize("hidden", [b"\xa0", b"\x85"], ids=["no-break-space", "nel"])
    async def test_a_name_behind_invisible_whitespace_is_not_ours(self, hidden: bytes) -> None:
        verification = FakeVerification()
        planted = httpx.Headers({b"cookie": hidden + f"{ROUND_TRIP_COOKIE}={PLANTED}".encode()})

        opened = await asked(verification, "/oauth/start", {"state": "S"}, planted)
        await asked(verification, "/oauth/discord/callback", {"code": "c", "state": "s"}, planted)
        await asked(verification, "/oauth/github/callback", {"code": "c", "state": "s"}, planted)

        value, _ = attributes_of(opened)
        assert value != PLANTED, "a planted cookie was handed back as a __Host- one"
        assert verification.started[0][1] == value
        assert verification.proved[0][2] == ""
        assert verification.redeemed[0][2] == ""

    async def test_a_name_that_arrives_twice_is_trusted_from_neither(self) -> None:
        """A browser holds one `__Host-` cookie of a name, so a second is somebody else's - and
        which of the two came first is not something to bet on."""
        verification = FakeVerification()
        twice = httpx.Headers(
            {"Cookie": f"{ROUND_TRIP_COOKIE}={BROWSER}; {ROUND_TRIP_COOKIE}={PLANTED}"}
        )

        opened = await asked(verification, "/oauth/start", {"state": "S"}, twice)
        await asked(verification, "/oauth/discord/callback", {"code": "c", "state": "s"}, twice)
        await asked(verification, "/oauth/github/callback", {"code": "c", "state": "s"}, twice)

        value, _ = attributes_of(opened)
        assert value not in (BROWSER, PLANTED)
        assert BROWSER_SHAPE.fullmatch(value)
        assert verification.proved[0][2] == ""
        assert verification.redeemed[0][2] == ""

    async def test_it_is_found_among_other_cookies(self) -> None:
        """Including a fragment with no `=` at all, which some clients send."""
        verification = FakeVerification()
        crowded = httpx.Headers(
            {"Cookie": f"theme=dark; flag; {ROUND_TRIP_COOKIE}={BROWSER} ;other=1"}
        )

        opened = await asked(verification, "/oauth/start", {"state": "S"}, crowded)
        await asked(verification, "/oauth/github/callback", {"code": "c", "state": "s"}, crowded)

        assert attributes_of(opened)[0] == BROWSER
        assert verification.redeemed[0][2] == BROWSER

    async def test_it_is_found_in_a_second_cookie_header(self) -> None:
        """HTTP/2 lets a browser split its cookies over several headers."""
        verification = FakeVerification()
        split = httpx.Headers(
            [("cookie", "theme=dark"), ("cookie", f"{ROUND_TRIP_COOKIE}={BROWSER}")]
        )

        await asked(verification, "/oauth/discord/callback", {"code": "c", "state": "s"}, split)

        assert verification.proved[0][2] == BROWSER


STOPS = [
    ("/oauth/start", {"state": "S"}),
    ("/oauth/discord/callback", {"code": "c", "state": "sealed"}),
    ("/oauth/github/callback", {"code": "c", "state": "sealed-again"}),
]


class TestADeploymentThatCannotSignInWithDiscord:
    """Failing closed, and saying it is the deployment rather than the link. The commands refuse to
    hand a link out here, so a browser arriving anyway has one from before the settings went."""

    @pytest.mark.parametrize(("path", "params"), STOPS)
    async def test_every_stop_refuses(self, path: str, params: dict[str, str]) -> None:
        verification = FakeVerification(can=False)

        async with browser_on(verification) as client:
            response = await client.get(path, params=params, headers=HOLDING)

        assert response.status_code == 500
        assert (verification.started, verification.proved, verification.redeemed) == ([], [], [])

    @pytest.mark.parametrize(("path", "params"), STOPS)
    async def test_so_does_one_with_no_service_at_all(
        self, path: str, params: dict[str, str]
    ) -> None:
        async with browser_on(None) as client:
            response = await client.get(path, params=params)

        assert response.status_code == 500
