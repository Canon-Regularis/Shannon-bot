"""Finding out who somebody actually is on GitHub, rather than who they say they are.

This is where the asking happens, and four purposes now depend on it. `/link` is finished by the
answer; `/register` and `/unregister` take it as evidence and then ask GitHub a second question,
about what that account may do to the repository.

For those three the user access token is used once and never written down - one lasts eight hours
and carries a six-month refresh token, so keeping it would be keeping something that outlives its
own usefulness.

**A board is the exception, and it is worth saying why rather than discovering it.** Issue #170. A
board is read every couple of seconds for as long as it is linked, with nobody at a keyboard to
consent again, so that authorisation has to outlive the browser visit that granted it. There is no
version of polling that keeps nothing. So a BOARD round trip hands its token to something that
encrypts it (`shannon.services.board_credentials`) and the other three still throw theirs away.

A board also runs against a DIFFERENT registered application - a classic OAuth App rather than the
GitHub App - and is the only purpose that asks GitHub for a scope. `OAuthClient` says why.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import (
    IdentityVerificationStore,
    ProvedAccount,
    VerifiedIdentityStore,
)
from shannon.domain.enums import VerificationPurpose
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


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """The application a round trip is run against, and what it asks for.

    There are two, and they are not interchangeable. Issue #170.

    Identity - `/link`, `/register`, `/unregister` - goes through the GitHub APP, which asks for no
    scope at all: a user token with the default empty scope can call `GET /user`, and that is the
    whole of what those three need.

    A board goes through a separate classic OAUTH APP, because GitHub publishes no App permission
    for a user-owned Projects v2 board - the Projects permission exists at organisation level only -
    and granting an installed App an organisation permission suspends its event delivery until an
    admin accepts, which would stop every webhook in every registered repository. OAuth scopes have
    no such problem and `project` covers user and organisation projects alike.

    Kept as a value rather than as four constructor arguments repeated twice, so that "which
    application, with which scope" is one thing a caller can hold and a test can substitute.
    """

    client_id: str
    client_secret: str
    # Empty for identity. GitHub treats a missing `scope` and an empty one differently from a
    # present one, so the parameter is omitted entirely rather than sent blank.
    scope: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


# A deployment that has registered no second application. Frozen, so one instance serves as the
# default for every such deployment; a constant rather than a call in an argument default, which
# is a thing ruff refuses on the general grounds that most such defaults are mutable.
NO_APPLICATION = OAuthClient(client_id="", client_secret="")

# What a board authorisation asks GitHub for. Read AND write, because moving a card is the feature
# on the other side of it: `read:project` would leave every `/status` refusing a write nobody could
# fix from Discord.
#
# Deliberately NOT a setting. Lowered, the board goes read-only and the failure appears somewhere
# else entirely, as a card that will not move; raised, it is a footgun with nothing asking for it.
# Least privilege here means least privilege for what the feature does, which is one fixed answer.
BOARD_SCOPE = "project"


class BindsProvedAccounts(Protocol):
    """Recording a GitHub account against a Discord one, where GitHub has vouched for it.

    Declared here because this is where it is now consumed: following a link is what links
    somebody, so the service that spends the link is the thing that needs to write the row.
    """

    async def bind(
        self, *, guild_id: int, discord_user_id: int, login: str, github_user_id: int
    ) -> str: ...


class KeepsBoardAuthorisations(Protocol):
    """Keeping the authorisation a board round trip grants, where the other three throw theirs away.

    Declared here because this is where it is consumed, which is the pattern `BindsProvedAccounts`
    above already sets. The difference between the two is the whole of why this exists: binding
    records a NAME GitHub vouched for, and this records a CREDENTIAL that acts as that account.

    Answers whether it was kept. A deployment with no encryption key cannot keep one, and the
    caller has just sent somebody through a browser - so it has to be able to say the trip was
    wasted rather than report a success that stored nothing.
    """

    async def remember(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        github_login: str,
        github_user_id: int,
        token: str,
    ) -> bool: ...


class VerificationError(ShannonError):
    """The round trip could not be completed, for a reason worth telling somebody about."""


@dataclass(frozen=True, slots=True)
class Verified:
    """Who GitHub says somebody is, which Discord account asked, and what that finishes.

    The purpose rides along because the callback route is the one place that has to tell a
    browser what to do next, and it has nothing else to decide that from.
    """

    guild_id: int
    discord_user_id: int
    login: str
    github_user_id: int
    purpose: VerificationPurpose


class GitHubIdentityVerification:
    """Issues the one-time links, and turns a redeemed one into a name GitHub vouched for."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        links: BindsProvedAccounts,
        boards: KeepsBoardAuthorisations,
        *,
        client_id: str,
        client_secret: str,
        oauth_url: str,
        public_base_url: str,
        http: httpx.AsyncClient,
        board: OAuthClient = NO_APPLICATION,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._links = links
        self._boards = boards
        # The App, for the three purposes that only need to know who somebody is. Kept as loose
        # arguments rather than folded into an `OAuthClient` so that every existing caller and
        # every existing fake reads exactly as it did; what is new here is the second client.
        self._identity = OAuthClient(client_id=client_id, client_secret=client_secret)
        self._board = board
        self._oauth_url = oauth_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._http = http
        self._now = now

    @property
    def configured(self) -> bool:
        """Whether this deployment can run the IDENTITY round trip at all.

        Deliberately still about the App alone. Six callers ask this before offering `/link`,
        `/register` or `/unregister`, and a deployment that has not registered the separate OAuth
        App for boards must not have those three refused along with it.
        """
        return bool(self._identity.configured and self._public_base_url)

    @property
    def can_authorise_a_board(self) -> bool:
        """Whether this deployment can run the BOARD round trip.

        Its own property rather than an argument to `configured`, for the reason above: the two
        applications are registered separately and either can be missing on its own.
        """
        return bool(self._board.configured and self._public_base_url)

    def _client_for(self, purpose: VerificationPurpose) -> OAuthClient:
        """Which application a purpose runs against.

        A one-line conditional rather than a mapping: a mapping would need an entry per purpose and
        a total-ness test to go with it, where the question here has exactly two answers and the
        default is the one three of the four purposes want.
        """
        return self._board if purpose is VerificationPurpose.BOARD else self._identity

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

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str:
        """A one-time authorize URL for this person in this server.

        The scope comes from the purpose, and for three of the four purposes there is none: a
        GitHub App's user token with the default empty scope can call `GET /user`, which is the
        whole of what the callback needs for an identity. A board is the exception - reading
        somebody's project board is a permission rather than a name - and it asks for `project`
        against a different application. See `OAuthClient`.

        The URL is a bearer credential, and the invariant that makes it safe is the caller's to
        keep: whoever opens it is recorded as `discord_user_id`, so every caller passes the id of
        the person in front of it and never one taken from an argument. Handing somebody a link
        issued for another member is handing them that member's identity.
        """
        client = self._client_for(purpose)
        state = secrets.token_urlsafe(STATE_BYTES)
        async with self._sessionmaker() as session, session.begin():
            await IdentityVerificationStore(session).issue(
                state=state,
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                purpose=purpose,
                lifetime=LINK_LIFETIME,
            )

        # Appended rather than sent empty, and as a one-line conditional so the coverage floor has
        # no second arm to ask about. GitHub reads a blank `scope` as a request for no scope, which
        # is the same thing as omitting it - but only omitting it says so plainly in the URL
        # somebody is about to be shown.
        asking = f"&scope={quote(client.scope, safe='')}" if client.scope else ""
        # `state` stays LAST, which is worth keeping deliberately rather than by accident: it is
        # the parameter everything downstream pulls back out of this URL, and anything appended
        # after it turns a naive split into a state with a scope stuck on the end. GitHub does not
        # care about the order.
        return (
            f"{self._oauth_url}/login/oauth/authorize"
            f"?client_id={client.client_id}"
            f"&redirect_uri={self.callback_url()}"
            f"{asking}"
            f"&state={state}"
        )

    async def redeem(self, *, state: str, code: str) -> Verified:
        """Spend a link, answer who followed it, and finish what the link was for.

        The state is consumed first and in one statement, so two clicks on one link race in the
        database rather than in Python and exactly one wins. Expired, already used and never
        issued give the same message: telling them apart would confirm a guessed state was real.

        The proof is recorded before anything is decided with it, and unconditionally. That
        ordering is what keeps `/unregister` untouched by this: it wants the proof and nothing
        else, so the write below is strictly additional rather than a branch it has to survive.

        A link finishes the job it was handed out for. `/link` is done here, because there is
        nothing left to ask: GitHub has just said which account this is and the person is holding
        the browser rather than Discord. `/unregister` is not, because what it does next is
        irreversible and needs somebody to report the answer to.

        If the bind fails after the state is spent, the proof stands and the link does not. The
        person runs the command again and gets a new one; the row they would have written is
        written then. Worth knowing rather than discovering.
        """
        async with self._sessionmaker() as session, session.begin():
            spent = await IdentityVerificationStore(session).consume(state)
        if spent is None:
            raise VerificationError(
                "That link has expired or has already been used. Run the command in Discord again."
            )

        # Against the application the purpose named, which the spent row has just told us. This is
        # why the state is consumed FIRST and not merely for the replay rule: the callback carries
        # no hint of which application issued the code, and exchanging against the wrong one answers
        # with an error rather than a token.
        token = await self._exchange(code, self._client_for(spent.purpose))
        login, github_user_id = await self._whoami(token)

        async with self._sessionmaker() as session, session.begin():
            await VerifiedIdentityStore(session).remember(
                guild_id=spent.guild_id,
                discord_user_id=spent.discord_user_id,
                github_login=login,
                github_user_id=github_user_id,
                verified_at=self._now(),
            )

        if spent.purpose is VerificationPurpose.LINK:
            await self._links.bind(
                guild_id=spent.guild_id,
                discord_user_id=spent.discord_user_id,
                login=login,
                github_user_id=github_user_id,
            )

        if spent.purpose is VerificationPurpose.BOARD and not await self._boards.remember(
            guild_id=spent.guild_id,
            discord_user_id=spent.discord_user_id,
            github_login=login,
            github_user_id=github_user_id,
            token=token,
        ):
            # The one purpose that can be granted and still fail, and it must say so. Everybody
            # else's token is finished with by here; a board's IS the thing being granted, so a
            # deployment that cannot keep it has sent somebody to GitHub for nothing - and worse,
            # left a real authorisation standing on their account with nothing here using it.
            # The message says that, because "it worked" would be a lie they could not check.
            raise VerificationError(
                "You authorised this, but this bot could not keep the authorisation, so the board "
                "will not be read. Nothing is wrong on your side - an admin needs to set "
                "SHANNON_BOARD_CREDENTIAL_KEY. You can withdraw the authorisation under "
                "Applications in your GitHub settings in the meantime."
            )

        logger.info("discord:%s proved they are github:%s", spent.discord_user_id, login)
        return Verified(
            guild_id=spent.guild_id,
            discord_user_id=spent.discord_user_id,
            login=login,
            github_user_id=github_user_id,
            purpose=spent.purpose,
        )

    async def prune(self, *, keep_for: timedelta) -> int:
        """Drop links long past being followable, answering how many went.

        `consume` marks a link spent but leaves the row, and a link nobody follows is never
        touched at all, so nothing here shrinks this table. Called on the delivery worker's
        hourly sweep, which is the only timer in the process that ticks regardless.
        """
        async with self._sessionmaker() as session, session.begin():
            return await IdentityVerificationStore(session).prune(keep_for=keep_for)

    async def _exchange(self, code: str, client: OAuthClient) -> str:
        """Trade the code for a user token.

        GitHub answers 200 with an error in the body for a bad code rather than a 4xx, so
        checking the status alone reads a refusal as a success.

        The token is still not kept HERE. Three of the four purposes are finished by knowing a
        name and the token is dropped on the way out of `redeem`; a board authorisation is the one
        that keeps it, and it does so deliberately and somewhere that encrypts it.
        """
        response = await self._http.post(
            f"{self._oauth_url}/login/oauth/access_token",
            data={
                "client_id": client.client_id,
                "client_secret": client.client_secret,
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
            raise VerificationError(
                "GitHub would not complete the sign-in. Try the command in Discord again."
            )
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
            raise VerificationError(
                "GitHub would not say who signed in. Try the command in Discord again."
            )
        return login, github_user_id
