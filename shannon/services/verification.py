"""Finding out who somebody actually is on GitHub, rather than who they say they are.

`/link` records a GitHub login against a Discord account after checking only that the login
exists. It is a claim somebody makes about themselves and it is unverified by construction, which
is fine for deciding who to mention and useless for deciding who may destroy a binding: any guild
administrator can link themselves to the repository owner's login and pass any check built on it.

So this asks GitHub. The person is sent a one-time link, they authorise the App, GitHub redirects
back naming them, and that name is what the permission check runs against.

The user access token is used once and thrown away. It is never written down anywhere. One lasts
eight hours and carries a six-month refresh token, so keeping either would mean holding a
credential to somebody's whole GitHub account in order to answer a question that has already been
answered.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import IdentityVerificationStore, VerifiedIdentityStore
from shannon.domain.errors import ShannonError
from shannon.github.responses import json_object

logger = logging.getLogger(__name__)

# How long a one-time link is worth following. Long enough to switch to a browser, log in and
# authorise; short enough that one left in a chat log is not a standing invitation to unbind
# somebody's repository.
LINK_LIFETIME = timedelta(minutes=10)

# How recently somebody must have proved themselves for it to still count. A proof costs a browser
# visit, so demanding one twice in a minute is a check people route around rather than use; a proof
# from an hour ago says little about now, and this one permits an irreversible command.
PROOF_LIFETIME = timedelta(minutes=15)

# 256 bits. This is the only thing tying an unauthenticated callback to the person who asked for
# it, so it is both the session identifier and the CSRF token.
STATE_BYTES = 32


class VerificationError(ShannonError):
    """The round trip could not be completed, for a reason worth telling somebody about."""


@dataclass(frozen=True, slots=True)
class Verified:
    """Who GitHub says somebody is, and which Discord account asked."""

    guild_id: int
    discord_user_id: int
    login: str
    github_user_id: int


class GitHubIdentityVerification:
    """Issues the one-time links, and turns a redeemed one into a name GitHub vouched for."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        client_id: str,
        client_secret: str,
        oauth_url: str,
        public_base_url: str,
        http: httpx.AsyncClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client_id = client_id
        self._client_secret = client_secret
        self._oauth_url = oauth_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._http = http
        self._now = now

    @property
    def configured(self) -> bool:
        """Whether this deployment can run the round trip at all.

        Asked before a link is offered, so a half-configured deployment says what is missing
        rather than handing somebody a URL that goes nowhere.
        """
        return bool(self._client_id and self._client_secret and self._public_base_url)

    def callback_url(self) -> str:
        return f"{self._public_base_url}/oauth/github/callback"

    async def already_proved(self, *, guild_id: int, discord_user_id: int) -> str | None:
        """The login this person proved recently, if they proved one recently."""
        async with self._sessionmaker() as session:
            return await VerifiedIdentityStore(session).fresh(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                newer_than=self._now() - PROOF_LIFETIME,
            )

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str:
        """A one-time authorize URL for this person in this server.

        No `scope` parameter. A GitHub App's user token with the default empty scope can call
        `GET /user`, which is the whole of what the callback needs, and asking for more would be
        asking somebody to grant access in order to prove they already have it.
        """
        state = secrets.token_urlsafe(STATE_BYTES)
        async with self._sessionmaker() as session, session.begin():
            await IdentityVerificationStore(session).issue(
                state=state,
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                lifetime=LINK_LIFETIME,
            )

        return (
            f"{self._oauth_url}/login/oauth/authorize"
            f"?client_id={self._client_id}"
            f"&redirect_uri={self.callback_url()}"
            f"&state={state}"
        )

    async def redeem(self, *, state: str, code: str) -> Verified:
        """Spend a link and answer who followed it.

        The state is consumed first and in one statement, so two clicks on one link race in the
        database rather than in Python and exactly one wins. Only then is anything spent on
        talking to GitHub.

        Every failure says the same thing. Expired, already used and never issued are identical
        to whoever is looking at the page, and telling them apart would confirm to somebody
        guessing states that a particular one was real.
        """
        async with self._sessionmaker() as session, session.begin():
            spent = await IdentityVerificationStore(session).consume(state)
        if spent is None:
            raise VerificationError(
                "That link has expired or has already been used. Run /unregister again."
            )

        guild_id, discord_user_id = spent
        token = await self._exchange(code)
        login, github_user_id = await self._whoami(token)

        async with self._sessionmaker() as session, session.begin():
            await VerifiedIdentityStore(session).remember(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                github_login=login,
                github_user_id=github_user_id,
                verified_at=self._now(),
            )

        logger.info("discord:%s proved they are github:%s", discord_user_id, login)
        return Verified(
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            login=login,
            github_user_id=github_user_id,
        )

    async def _exchange(self, code: str) -> str:
        """Trade the code for a user token, and never keep it.

        GitHub answers **200 with an error in the body** for a bad code rather than a 4xx, which
        is the single easiest thing to get wrong here: checking the status alone reads a refusal
        as a success and then fails further along with something unrelated to say.
        """
        response = await self._http.post(
            f"{self._oauth_url}/login/oauth/access_token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self.callback_url(),
            },
            headers={"Accept": "application/json"},
        )
        payload = json_object(response)
        token = payload.get("access_token")
        if response.status_code >= 400 or payload.get("error") or not isinstance(token, str):
            # The reason GitHub gave is deliberately not echoed. It is written for a developer
            # debugging an OAuth app, not for somebody who clicked a link, and it can carry the
            # code back out into a page.
            logger.warning("the oauth exchange failed: %s", payload.get("error") or "no token")
            raise VerificationError("GitHub would not complete the sign-in. Try /unregister again.")
        return token

    async def _whoami(self, token: str) -> tuple[str, int]:
        """The one call the user token is for."""
        response = await self._http.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        payload = json_object(response)
        login = payload.get("login")
        github_user_id = payload.get("id")
        if not isinstance(login, str) or not login or not isinstance(github_user_id, int):
            raise VerificationError("GitHub would not say who signed in. Try /unregister again.")
        return login, github_user_id
