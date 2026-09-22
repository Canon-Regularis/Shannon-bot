"""Finding out who somebody actually is on GitHub, rather than who they say they are.

`/link` records an unverified claim: any guild administrator can link themselves to the repository
owner's login, so this asks GitHub instead. The user access token is used once and never written
down - one lasts eight hours and carries a six-month refresh token.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import (
    IdentityVerificationStore,
    ProvedAccount,
    VerifiedIdentityStore,
)
from shannon.domain.errors import ShannonError
from shannon.github.responses import json_object

logger = logging.getLogger(__name__)

# How long a one-time link is worth following. Short because one left in a chat log is a standing
# invitation to unbind somebody's repository.
LINK_LIFETIME = timedelta(minutes=10)

# How recently somebody must have proved themselves for it to still count. A proof costs a browser
# visit, so too short a window is a check people route around rather than use.
PROOF_LIFETIME = timedelta(minutes=15)

# 256 bits: the state is the only thing tying an unauthenticated callback to the person who asked
# for it, so it is both the session identifier and the CSRF token.
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
        """Whether this deployment can run the round trip at all."""
        return bool(self._client_id and self._client_secret and self._public_base_url)

    def callback_url(self) -> str:
        return f"{self._public_base_url}/oauth/github/callback"

    async def proved_just_now(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        """The account this person proved within the last few minutes, if they proved one.

        What a command asks between handing out a link and acting on it: the person went away to a
        browser and came back, and this is how the second run knows the first one landed.
        """
        async with self._sessionmaker() as session:
            return await VerifiedIdentityStore(session).proved(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                newer_than=self._now() - PROOF_LIFETIME,
            )

    async def ever_proved(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        """The account this person has proved they hold, however long ago that was.

        Two methods rather than one taking a window, because the difference is not a parameter: it
        is the difference between asking permission for something irreversible and asking whether a
        stored link was ever anything more than somebody's say-so. The second does not go stale.
        A name somebody proved and then gave up is not a name anybody else can have quietly, since
        what is held against the link is the account id rather than the name.
        """
        async with self._sessionmaker() as session:
            return await VerifiedIdentityStore(session).proved(
                guild_id=guild_id, discord_user_id=discord_user_id
            )

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str:
        """A one-time authorize URL for this person in this server.

        No `scope` parameter: a GitHub App's user token with the default empty scope can call
        `GET /user`, which is the whole of what the callback needs.
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
        database rather than in Python and exactly one wins. Expired, already used and never
        issued give the same message: telling them apart would confirm a guessed state was real.
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

    async def prune(self, *, keep_for: timedelta) -> int:
        """Drop links long past being followable, answering how many went.

        `consume` marks a link spent but leaves the row, and a link nobody follows is never
        touched at all, so nothing here shrinks this table. Called on the delivery worker's
        hourly sweep, which is the only timer in the process that ticks regardless.
        """
        async with self._sessionmaker() as session, session.begin():
            return await IdentityVerificationStore(session).prune(keep_for=keep_for)

    async def _exchange(self, code: str) -> str:
        """Trade the code for a user token, and never keep it.

        GitHub answers 200 with an error in the body for a bad code rather than a 4xx, so
        checking the status alone reads a refusal as a success.
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
            # GitHub's reason is not echoed: it is written for a developer debugging an OAuth
            # app, and it can carry the code back out into a page.
            logger.warning("the oauth exchange failed: %s", payload.get("error") or "no token")
            raise VerificationError("GitHub would not complete the sign-in. Try /unregister again.")
        return token

    async def _whoami(self, token: str) -> tuple[str, int]:
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
