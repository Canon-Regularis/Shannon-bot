"""Following a one-time link the way a browser does, for every test that needs one followed.

Found reviewing #201. A link no longer goes straight to GitHub: it opens on this bot, which leaves a
cookie and sends the browser to Discord; Discord says which member is holding the browser; and only
then does the browser go on to GitHub, carrying a state sealed to its cookie. A test that used to
hand a link's state straight to `redeem` now walks the same three steps, and this is where they are
walked - so each of those tests still reads as "the link was followed" rather than as a protocol.

Discord is a stand-in on the same transport as GitHub. `discord_says` answers Discord's two calls,
answers any other Discord path with a 404 and hands every request for another host on; the code
`as:<id>` signs in the member `<id>`, so a test says who followed a link by the code it passes -
which is how a forwarded link is written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.api.routes import oauth
from shannon.services.board_credentials import BoardCredentials
from shannon.services.linking import UserLinkingService
from shannon.services.verification import (
    DISCORD_SCOPE,
    NO_APPLICATION,
    GitHubIdentityVerification,
    OAuthClient,
    Verified,
)
from tests.fakes.board_links import NoBoardLinks

# The bot's own Discord application, which every test that builds the real service itself gives
# it - except a test about a deployment without one, which passes `NO_APPLICATION` instead, as
# `without_discord` below does.
DISCORD_APP = OAuthClient(
    client_id="1180000000000000", client_secret="discord-shh", scope=DISCORD_SCOPE
)

# Two browsers' cookies. Well-formed, so the service makes keys of them; two, for the tests about a
# round trip arriving somewhere other than the browser it began in.
BROWSER = "b" * 43
OTHER_BROWSER = "c" * 43

Handler = Callable[[httpx.Request], httpx.Response]


def without_discord(
    sessionmaker: async_sessionmaker[AsyncSession], http: httpx.AsyncClient
) -> GitHubIdentityVerification:
    """A deployment with the App set up and no Discord sign-in: `configured`, and nothing more.

    What every deployment is between upgrading past #201's fix and setting the two Discord
    settings, and the state in which `SHANNON_REQUIRE_PROVED_LINKS` must go on being enforced.
    """
    return GitHubIdentityVerification(
        sessionmaker,
        UserLinkingService(sessionmaker),
        BoardCredentials(sessionmaker, keys=""),
        board_links=NoBoardLinks(),
        discord=NO_APPLICATION,
        client_id="Iv23liAbC",
        client_secret="shh",
        oauth_url="https://github.com",
        public_base_url="https://shannon.example.com",
        http=http,
    )


def signs_in(member: int) -> str:
    """The Discord code that signs in `member`, as `discord_says` reads it."""
    return f"as:{member}"


def discord_says(then: Handler) -> Handler:
    """Discord, in front of whatever answers for GitHub.

    The token endpoint answers a code `as:<id>` with a token for `<id>`, and `/users/@me` answers
    that token with the id. Any other code is refused the way Discord refuses one, with a 400 and
    `invalid_grant`, and any other path is a 404 - so a wrong URL in the service fails every round
    trip in the suite rather than none of them. Nothing that reaches Discord is handed on, so a
    GitHub handler that records what it was asked records GitHub alone.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "discord.com":
            return then(request)
        if request.url.path == "/api/v10/oauth2/token":
            code = parse_qs(request.content.decode())["code"][0]
            if not code.startswith("as:"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": f"discord-{code[3:]}", "token_type": "Bearer"}
            )
        if request.url.path != "/api/v10/users/@me":
            return httpx.Response(404, json={"message": "404: Not Found", "code": 0})
        member = request.headers["Authorization"].removeprefix("Bearer discord-")
        return httpx.Response(200, json={"id": member, "username": f"member{member}"})

    return handler


def state_of(url: str) -> str:
    """The state a link or a redirect carries. Last in every one of them, by design."""
    return url.partition("state=")[2]


@dataclass(frozen=True, slots=True)
class AtGitHub:
    """Where a browser stands once Discord has let it through: on GitHub's authorize page."""

    url: str
    browser: str

    @property
    def state(self) -> str:
        """The sealed state GitHub hands back to the callback."""
        return state_of(self.url)


async def to_discord(
    verification: GitHubIdentityVerification, link: str, *, browser: str = BROWSER
) -> str:
    """Open a link: the Discord authorize page the browser is sent to."""
    return await verification.start(state=state_of(link), browser=browser)


async def to_github(
    verification: GitHubIdentityVerification,
    link: str,
    *,
    member: int,
    browser: str = BROWSER,
) -> AtGitHub:
    """Open a link and sign in to Discord as `member`, landing on GitHub's authorize page."""
    discord = await to_discord(verification, link, browser=browser)
    github = await verification.prove_on_discord(
        state=state_of(discord), code=signs_in(member), browser=browser
    )
    return AtGitHub(url=github, browser=browser)


async def round_trip(
    verification: GitHubIdentityVerification,
    link: str,
    *,
    member: int,
    code: str = "abc",
    browser: str = BROWSER,
) -> Verified:
    """Follow a link all the way: through Discord as `member`, then GitHub with `code`."""
    at = await to_github(verification, link, member=member, browser=browser)
    return await verification.redeem(state=at.state, code=code, browser=at.browser)


@asynccontextmanager
async def browser_on(verification: object) -> AsyncIterator[httpx.AsyncClient]:
    """The three OAuth routes on a bare app, and one browser pointed at them.

    One client is one cookie jar, which is one browser. On https, because the round-trip cookie is
    Secure and a browser - httpx's jar included - sends it back over nothing else. A bare app
    rather than `create_app`, because the routes read one thing off app state and nothing more.
    """
    app = FastAPI()
    app.state.verification = verification
    app.include_router(oauth.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        yield client


async def to_github_in(client: httpx.AsyncClient, link: str, *, member: int) -> str:
    """Open a link in a browser and sign in to Discord as `member`: GitHub's authorize URL.

    Through the routes, so the cookie is the one the route set and the browser sent back.
    """
    opened = await client.get("/oauth/start", params={"state": state_of(link)})
    assert opened.status_code == 303, opened.text
    back = await client.get(
        "/oauth/discord/callback",
        params={"code": signs_in(member), "state": state_of(opened.headers["location"])},
    )
    assert back.status_code == 303, back.text
    return back.headers["location"]


async def followed_in(
    client: httpx.AsyncClient, link: str, *, member: int, code: str = "abc"
) -> httpx.Response:
    """Follow a link all the way in one browser, answering the page GitHub's callback shows."""
    github = await to_github_in(client, link, member=member)
    return await client.get(
        "/oauth/github/callback", params={"code": code, "state": state_of(github)}
    )
