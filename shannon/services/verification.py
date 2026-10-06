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

**A link proves who is holding it before it proves anything else.** Found reviewing #201. A link
used to point straight at GitHub and its callback trusted nothing but the state, and GitHub skips
its consent page for an application somebody has already authorised - so a link forwarded to such
a person signed its ISSUER in as them, without them seeing a thing. Every purpose shared it, and
for a board the prize was a `project` token that acts as the person who clicked.

So a link opens on this bot first. It leaves a cookie in the browser and sends it to Discord,
which says which account is holding that browser; only if that is the member the link was issued
for is the browser written down, and only that browser may then finish at GitHub. Every state
that leaves this bot is SEALED to the cookie - `S.mac`, an HMAC keyed by the cookie - so nobody
can push their own half-finished round trip into somebody else's browser: they cannot compute the
seal for a cookie they do not hold. No server-side secret is needed for that, and nothing in the
database could be replayed: the row holds a hash keyed by the cookie, never the cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import (
    IdentityVerificationStore,
    PendingLink,
    ProvedAccount,
    SpentLink,
    VerifiedIdentityStore,
)
from shannon.db.stores.repositories import RepositoryStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.roles import CommandRole
from shannon.domain.board import ChosenBoard, board_owner
from shannon.domain.enums import VerificationPurpose
from shannon.domain.errors import ShannonError
from shannon.github.responses import json_object
from shannon.services.boards import BoardLink

logger = logging.getLogger(__name__)

# How long a one-time link is worth following, and the round-trip cookie with it. Short, although
# since #201's review a link found in a chat log signs nobody in but whoever it was issued for: what
# it bounds now is how long a row and a cookie stay live, and a browser visit needs minutes.
LINK_LIFETIME = timedelta(minutes=10)

# How recently somebody must have proved themselves for it to still count. A proof costs a browser
# visit, so too short a window is a check people route around rather than use.
PROOF_LIFETIME = timedelta(minutes=15)

# 256 bits. The state names the row a round trip is about; since #201's review it is no longer
# the whole of the proof - the browser that went through Discord is - but it is still the only
# way back to the row from a callback, so it stays unguessable.
STATE_BYTES = 32

# Discord, for the half of a round trip that asks who is holding the browser. Found reviewing
# #201. Versioned, because an unversioned path is answered by a deprecated default version.
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/v10/oauth2/token"
DISCORD_ME = "https://discord.com/api/v10/users/@me"
# All that is asked of Discord is which account this is: no email, no guilds, nothing kept.
# Deliberately not a setting, for the reason `BOARD_SCOPE` below is not one.
DISCORD_SCOPE = "identify"

# The browser's half of a round trip: 256 bits, minted by the route a link opens on and held in a
# cookie only that browser sends back. Its shape is checked before it is ever used as a key,
# because a missing cookie would otherwise be an empty HMAC key - and anybody can compute with
# that.
BROWSER_BYTES = 32
BROWSER_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")

# What `link_for` mints: `token_urlsafe(STATE_BYTES)`, always these 43 characters. A state out of a
# query string is held to it before it goes anywhere near the database. Found reviewing #201's fix:
# Postgres answers a NUL byte in a text parameter with an error rather than with no row, which made
# a 500 and a traceback out of a request to `/oauth/start` that needed nothing at all.
STATE_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")

# What a browser is told when a round trip cannot go on. One sentence for expired, spent, never
# issued and finished in another browser alike, for the reason `consume` gives: telling them
# apart would confirm to somebody guessing that a particular state was real.
UNFOLLOWABLE = (
    "That link has expired or has already been used, or it was opened in another browser. Run "
    "the command in Discord again."
)

# The Discord half arrived in a browser that did not start it. The commonest honest cause is the
# Discord app on a phone taking the authorize page over and handing back to another browser.
NOT_THIS_BROWSER = (
    "This sign-in has to finish in the browser it began in, and this is not that browser. If "
    "Discord opened its own app part of the way through, open the link from Discord again and "
    "paste it into your usual browser rather than tapping it."
)

# Discord names somebody other than the member the link was issued for: a forwarded link, which
# is the thing this whole round trip now exists to stop.
NOT_YOURS = (
    "This link was made for somebody else, and Discord says you are not them, so it signed nobody "
    "in and nothing was recorded. To connect your own account, run the command in Discord "
    "yourself - or, if you did run it, sign in to Discord in this browser as that account."
)

DISCORD_REFUSED = "Discord would not complete the sign-in. Try the command in Discord again."

DISCORD_SILENT = "Discord would not say who signed in. Try the command in Discord again."

# A board link followed by somebody who has lost, since it was handed out, the role the command
# was gated on. Found reviewing #201. Nothing was kept here, but GitHub holds the grant on its side
# all the same, and only they can take that back.
NO_LONGER_ALLOWED = (
    "You no longer hold a role in that server that may do this, so nothing was kept and no board "
    "was linked. GitHub may still list this app under Applications in your GitHub settings, where "
    "you can revoke it."
)

# Discord could not be asked whether they still hold it. A refusal all the same: "could not check"
# is not "allowed".
CANNOT_ASK = (
    "Discord could not be asked whether you still hold the role this needs, so nothing was kept. "
    "Try the command in Discord again in a minute."
)


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

    Since #201's review there is a third, and it is not GitHub at all: the bot's own Discord
    application, asked for `identify` and nothing else, so that a link can say which Discord
    account is holding the browser before GitHub is asked anything.

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
#
# Asked for is not granted. GitHub lets a person grant less than was asked and issues the token
# anyway, so `_exchange` holds what came back against this and keeps nothing short of it. Issue
# #201: until then a short grant was kept, and found out later as a board that would not open or
# a card that would not move.
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

    And says beforehand whether it could, which is the better place to find out. `usable` is read
    before anybody is offered the trip at all; the answer from `remember` is for a key that went
    away between the link being handed out and being followed.
    """

    @property
    def usable(self) -> bool: ...

    async def remember(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        github_login: str,
        github_user_id: int,
        token: str,
    ) -> bool: ...


class HoldsTiers(Protocol):
    """Whether a member holds any of a set of tiers in a server, asked of Discord now.

    Declared here because this is where it is consumed, the pattern `BindsProvedAccounts` sets.
    Found reviewing #201: a board link is followed up to ten minutes after the command checked the
    role, and following it keeps a credential that acts as the member. Raises `DiscordGatewayError`
    where Discord cannot be asked, which the caller treats as a refusal.
    """

    async def holds(
        self, *, guild_id: int, discord_user_id: int, tiers: Collection[CommandRole]
    ) -> bool: ...


class LinksTheBoardChosen(Protocol):
    """Linking the board a one-click link was issued for, as the member who followed it.

    Declared here because this is where it is consumed, the pattern `BindsProvedAccounts` above
    sets: following a LINK link is what binds somebody, and since issue #201 following a BOARD link
    that carries a board is what links it.
    """

    async def assign(
        self,
        *,
        guild_id: int,
        project_number: int,
        typed_owner: str,
        acting: int,
        chosen_under: str | None = None,
    ) -> BoardLink: ...


@dataclass(frozen=True, slots=True)
class BoardLinked:
    """The board a one-click link carried, mirrored now."""

    link: BoardLink


@dataclass(frozen=True, slots=True)
class BoardNotLinked:
    """The board a one-click link carried, and why it is not mirrored.

    The authorisation was kept all the same. The reason is a sentence for a person in a browser,
    or empty where it is nothing they could act on - the log has that one.
    """

    reason: str


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
    # What became of the board a BOARD link carried, and None for any link that carried none -
    # which is every identity link and every `/board authorise`. Issue #201.
    board: BoardLinked | BoardNotLinked | None = None


class GitHubIdentityVerification:
    """Issues the one-time links, and turns a redeemed one into a name GitHub vouched for."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        links: BindsProvedAccounts,
        boards: KeepsBoardAuthorisations,
        *,
        board_links: LinksTheBoardChosen,
        tiers: HoldsTiers,
        discord: OAuthClient,
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
        self._board_links = board_links
        # Who still holds the tier a board link was handed out under. Required for the reason
        # `discord` below is: a default would be a way to follow a board link without asking.
        self._tiers = tiers
        # The App, for the three purposes that only need to know who somebody is. Kept as loose
        # arguments rather than folded into an `OAuthClient` so that every existing caller and
        # every existing fake reads exactly as it did; what is new here is the second client.
        self._identity = OAuthClient(client_id=client_id, client_secret=client_secret)
        self._board = board
        # The bot's own Discord application, which every round trip now goes through first.
        # Required rather than defaulted: a default would be a way for a deployment to hand out
        # links that skip it, and skipping it is the hole it closes.
        self._discord = discord
        self._oauth_url = oauth_url.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._http = http
        self._now = now

    @property
    def configured(self) -> bool:
        """Whether the App's half of the IDENTITY round trip is set up: its id, secret and a URL.

        Deliberately the App alone, and no longer what the commands ask. Since #201's review a link
        needs Discord as well, so `/link`, `/register` and `/unregister` read `can_prove_identity`
        below. What still reads this is `access.py` and `people.py`, deciding whether
        `SHANNON_REQUIRE_PROVED_LINKS` is enforced - which a missing Discord half must not switch
        off.
        """
        return bool(self._identity.configured and self._public_base_url)

    @property
    def can_sign_in_with_discord(self) -> bool:
        """Whether a round trip can ask Discord who is holding the browser.

        Found reviewing #201, and asked by the routes as well as the commands: without it no link
        can be followed at all, because no browser could ever be bound to the member it was
        issued for. That is the fail-closed answer, and it was chosen.
        """
        return bool(self._discord.configured and self._public_base_url)

    @property
    def can_prove_identity(self) -> bool:
        """Whether `/link`, `/register` and `/unregister` may hand out a link.

        Both applications, because a link now needs both halves: Discord to say who is holding
        the browser, and the App to say which GitHub account that is.

        Its own property rather than a change to `configured`, which two services read to decide
        whether `SHANNON_REQUIRE_PROVED_LINKS` is enforced. Folding Discord into that would turn
        the enforcement OFF on a deployment that has not configured Discord yet - failing open,
        which is the opposite of what refusing until it is set up was for.
        """
        return self.configured and self.can_sign_in_with_discord

    @property
    def can_authorise_a_board(self) -> bool:
        """Whether this deployment can run the BOARD round trip, and keep what it grants.

        Its own property rather than an argument to `configured`, for the reason above: the two
        applications are registered separately and either can be missing on its own.

        The key is part of the question, because a board's authorisation is the one that has to
        be kept. Without `SHANNON_BOARD_CREDENTIAL_KEY` the round trip runs perfectly well and then
        cannot store its answer, so somebody would be sent to GitHub to grant something that is
        thrown away, and told so only once they were back. Issue #201. Asking first means the
        command refuses before the trip rather than after it.
        """
        return bool(
            self._board.configured and self._boards.usable and self.can_sign_in_with_discord
        )

    def _client_for(self, purpose: VerificationPurpose) -> OAuthClient:
        """Which application a purpose runs against.

        A one-line conditional rather than a mapping: a mapping would need an entry per purpose and
        a total-ness test to go with it, where the question here has exactly two answers and the
        default is the one three of the four purposes want.
        """
        return self._board if purpose is VerificationPurpose.BOARD else self._identity

    def callback_url(self) -> str:
        return f"{self._public_base_url}/oauth/github/callback"

    def discord_callback_url(self) -> str:
        return f"{self._public_base_url}/oauth/discord/callback"

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
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        purpose: VerificationPurpose,
        board: ChosenBoard | None = None,
        tier: Collection[CommandRole] | None = None,
    ) -> str:
        """A one-time link for this person in this server, which opens on this bot.

        It opens HERE rather than at GitHub. Found reviewing #201: a link that pointed straight at
        GitHub was finished by whoever clicked it, recorded as the person it was issued for - and
        GitHub skips its consent page for an application somebody has already authorised, so a
        forwarded link signed its issuer in as the person they forwarded it to without that person
        seeing anything. `/oauth/start` sends the browser to Discord first, and only the member the
        link was issued for can take it any further. The state is in the link because it names the
        row; holding it no longer completes anything.

        The invariant callers keep is unchanged and still worth keeping: whoever the link is issued
        for is `discord_user_id`, so every caller passes the id of the person in front of it and
        never one taken from an argument. What changed is that a link issued for somebody can now
        only ever be finished BY them.

        `board` is the board a board link is handed out to link, where it is handed out for one.
        Issue #201: one link both authorises and links, so the choice has to survive the trip to
        GitHub and back. It is written on the row beside the purpose and never into the URL.

        `tier` is the set of tiers the command handing a board link out was gated on, and the one
        `redeem` asks Discord about again. Found reviewing #201. Written on the row for the same
        reason the board is. An identity link carries none: a `/register` or `/unregister` link
        records a proof and nothing more, and running the command again asks for the role again,
        and a `/link` link binds the account, but linking yourself takes no role.

        A board chosen by a bare number is written down with whose board that number meant now:
        the repository's owner, which following the link checks is still its owner. Found
        reviewing #201 - see `IdentityVerification.board_chosen_under`. Read in the transaction
        that hands the link out, so the two are one moment.
        """
        state = secrets.token_urlsafe(STATE_BYTES)
        async with self._sessionmaker() as session, session.begin():
            chosen_under = None
            if board is not None and not board.owner:
                repository = await RepositoryStore(session).get_by_guild(guild_id)
                if repository is not None:
                    chosen_under = board_owner(project_owner=None, repo_name=repository.repo_name)
            await IdentityVerificationStore(session).issue(
                state=state,
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                purpose=purpose,
                lifetime=LINK_LIFETIME,
                board=board,
                tier=None if tier is None else frozenset(role.value for role in tier),
                chosen_under=chosen_under,
            )
        return f"{self._public_base_url}/oauth/start?state={state}"

    async def start(self, *, state: str, browser: str) -> str:
        """Where a browser that has just opened a link goes next: Discord's authorize page.

        Writes nothing. A link preview, a crawler, or the same person opening the link twice all
        arrive here, and none of them may spend, bind or otherwise change the row - all this decides
        is whether there is anything to send somebody to Discord about.

        `prompt=none` because a member who has authorised this bot's Discord application before has
        nothing to read on that page again; somebody who has not still sees it, once. The state
        handed to Discord is sealed to this browser, so the half that comes back can be finished by
        the browser that started it and by no other.
        """
        if STATE_SHAPE.fullmatch(state) is None:
            raise VerificationError(UNFOLLOWABLE)
        async with self._sessionmaker() as session:
            pending = await IdentityVerificationStore(session).pending(state)
        if pending is None:
            raise VerificationError(UNFOLLOWABLE)
        return (
            f"{DISCORD_AUTHORIZE}"
            f"?client_id={self._discord.client_id}"
            f"&redirect_uri={quote(self.discord_callback_url(), safe='')}"
            "&response_type=code"
            f"&scope={quote(self._discord.scope, safe='')}"
            "&prompt=none"
            f"&state={_sealed(browser, 'discord', state)}"
        )

    async def prove_on_discord(self, *, state: str, code: str, browser: str) -> str:
        """Ask Discord who is holding this browser, and send it on to GitHub only if that is the
        member the link was issued for.

        In this order, and each step for a reason:

        1. The seal is opened first. A Discord code pushed into somebody else's browser - the
           issuer's own, from a round trip they started and kept back - carries a seal for a cookie
           that browser does not hold, so it is refused before anything is asked of anybody.
        2. The link must still be followable before Discord's code is spent on it.
        3. The code is exchanged and the token used once, for `/users/@me`, then dropped. It is
           never stored, logged or revoked: it reads nothing but a name, and it expires on its own.
        4. The account Discord names must be the member the link was issued for. Anybody else is
           following somebody else's link - a forwarded one, which this exists to stop - and
           nothing is written.
        5. The browser is bound to the row, as a hash keyed by its cookie.
        6. On to GitHub, with the state sealed to this browser again, so the GitHub page that
           results is as useless anywhere else as the Discord one was.
        """
        opened = _opened(browser, "discord", state)
        if opened is None:
            raise VerificationError(NOT_THIS_BROWSER)
        async with self._sessionmaker() as session:
            pending = await IdentityVerificationStore(session).pending(opened)
        if pending is None:
            raise VerificationError(UNFOLLOWABLE)

        member = await self._discord_member(code)
        if member != pending.discord_user_id:
            self._refuse_a_forwarded_link(pending)

        async with self._sessionmaker() as session, session.begin():
            bound = await IdentityVerificationStore(session).bind(
                opened, discord_user_id=member, binding=_binding(browser, opened)
            )
        if not bound:
            raise VerificationError(UNFOLLOWABLE)
        return self._github_authorize_url(_sealed(browser, "github", opened), pending.purpose)

    def _refuse_a_forwarded_link(self, pending: PendingLink) -> NoReturn:
        """Say no to a link followed by somebody it was not issued for, and log the issuer.

        The issuer and the server only. Whoever followed somebody else's link was very likely sent
        it on purpose by someone else, and writing their Discord id down would be keeping a fact
        about them they never agreed to give this bot.
        """
        logger.warning(
            "a link issued for discord:%s in guild %s was followed by a different Discord "
            "account, so nothing was recorded",
            pending.discord_user_id,
            pending.guild_id,
        )
        raise VerificationError(NOT_YOURS)

    def _github_authorize_url(self, state: str, purpose: VerificationPurpose) -> str:
        """GitHub's authorize page for one round trip, carrying a state sealed to the browser.

        The scope comes from the purpose, and for three of the four purposes there is none: a
        GitHub App's user token with the default empty scope can call `GET /user`, which is the
        whole of what the callback needs for an identity. A board is the exception - reading
        somebody's project board is a permission rather than a name - and it asks for `project`
        against a different application. See `OAuthClient`.
        """
        client = self._client_for(purpose)
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

    async def redeem(self, *, state: str, code: str, browser: str) -> Verified:
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

        Only by the browser that proved itself through Discord, and the state arriving here is the
        sealed one `prove_on_discord` sent to GitHub. Found reviewing #201. A seal that does not
        open under this browser's cookie - a GitHub link forwarded to somebody else, or somebody's
        own GitHub code pushed into a victim's browser - is refused before anything is spent.
        """
        opened = _opened(browser, "github", state)
        if opened is None:
            raise VerificationError(UNFOLLOWABLE)
        async with self._sessionmaker() as session, session.begin():
            spent = await IdentityVerificationStore(session).consume(
                opened, binding=_binding(browser, opened)
            )
        if spent is None:
            raise VerificationError(UNFOLLOWABLE)
        # Before the code is exchanged, so a refusal keeps nothing at all.
        if spent.purpose is VerificationPurpose.BOARD:
            await self._still_holds_the_tier(spent)

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

        board: BoardLinked | BoardNotLinked | None = None
        if spent.purpose is VerificationPurpose.BOARD:
            if not await self._boards.remember(
                guild_id=spent.guild_id,
                discord_user_id=spent.discord_user_id,
                github_login=login,
                github_user_id=github_user_id,
                token=token,
            ):
                # The one purpose that can be granted and still fail, and it must say so. Everybody
                # else's token is finished with by here; a board's IS the thing being granted, so a
                # deployment that cannot keep it has sent somebody to GitHub for nothing - and
                # worse, left a real authorisation standing on their account with nothing here
                # using it. The message says that, because "it worked" would be a lie they could
                # not check.
                raise VerificationError(
                    "You authorised this, but this bot could not keep the authorisation, so the "
                    "board will not be read. Nothing is wrong on your side - an admin needs to set "
                    "SHANNON_BOARD_CREDENTIAL_KEY. You can withdraw the authorisation under "
                    "Applications in your GitHub settings in the meantime."
                )
            # After the authorisation is kept and never before: linking opens the board WITH it.
            board = await self._link_what_was_chosen(spent)

        logger.info("discord:%s proved they are github:%s", spent.discord_user_id, login)
        return Verified(
            guild_id=spent.guild_id,
            discord_user_id=spent.discord_user_id,
            login=login,
            github_user_id=github_user_id,
            purpose=spent.purpose,
            board=board,
        )

    async def _still_holds_the_tier(self, spent: SpentLink) -> None:
        """Ask Discord, now, whether the member still holds the tier the command was gated on.

        Found reviewing #201. A board link does something the moment it is followed - it keeps a
        credential that acts as the member, and for `/board link` points the server at a board -
        and the role was checked when the command ran, up to ten minutes before. A role taken away
        in between takes the link with it.

        Failing closed: where Discord cannot be asked, that is a refusal too, since "could not
        check" is not "allowed". A tier this code no longer knows is dropped rather than guessed
        at, which narrows the question towards administrators, who hold every tier; and a board
        link from before the tier was written down is asked about as administrators only, for the
        same reason and for ten minutes at most.
        """
        tiers = _known_tiers(spent.tier or frozenset())
        try:
            held = await self._tiers.holds(
                guild_id=spent.guild_id, discord_user_id=spent.discord_user_id, tiers=tiers
            )
        except DiscordGatewayError as unreachable:
            logger.warning(
                "could not ask Discord whether discord:%s may still finish a board link in guild "
                "%s: %s",
                spent.discord_user_id,
                spent.guild_id,
                unreachable,
            )
            raise VerificationError(CANNOT_ASK) from unreachable
        if not held:
            logger.info(
                "discord:%s no longer holds the tier a board link was handed out under in guild "
                "%s, so nothing was kept",
                spent.discord_user_id,
                spent.guild_id,
            )
            raise VerificationError(NO_LONGER_ALLOWED)

    async def _link_what_was_chosen(self, spent: SpentLink) -> BoardLinked | BoardNotLinked | None:
        """Point the server at the board a one-click link was issued for, as the member it names.

        Issue #201. `/board link` used to be refused without an authorisation and the person sent
        off to a second command, to GitHub, and back to run the first one again. Now the link it
        hands out remembers the board, and this is where following it finishes the job - the shape
        `/link` already had, where clicking the link is the whole of it.

        As `spent.discord_user_id` and nobody else: the member the row was issued for, which the
        command only ever sets to whoever ran it. The board comes off the same row, which never
        left the database, so nothing on the way to GitHub and back could have changed it.

        A refusal is an answer rather than a failure. The person did authorise, and that stands -
        it is what their cards are moved with and what the next `/board link` opens the board
        with - so the page says what happened to the board and the grant is kept. Anything else is
        logged and folded into the same answer: the state is spent and the grant is kept, so a
        server error here would be shown to somebody whose sign-in worked.
        """
        if spent.board is None:
            return None
        try:
            linked = await self._board_links.assign(
                guild_id=spent.guild_id,
                project_number=spent.board.number,
                typed_owner=spent.board.owner,
                acting=spent.discord_user_id,
                chosen_under=spent.chosen_under,
            )
        except ShannonError as refusal:
            return BoardNotLinked(reason=refusal.message)
        except Exception:
            logger.exception(
                "discord:%s authorised a board in guild %s, and linking it then failed",
                spent.discord_user_id,
                spent.guild_id,
            )
            return BoardNotLinked(reason="")
        return BoardLinked(link=linked)

    async def prune(self, *, keep_for: timedelta) -> int:
        """Drop links long past being followable, answering how many went.

        `consume` marks a link spent but leaves the row, and a link nobody follows is never
        touched at all, so nothing here shrinks this table. Called on the delivery worker's
        hourly sweep, which is the only timer in the process that ticks regardless.
        """
        async with self._sessionmaker() as session, session.begin():
            return await IdentityVerificationStore(session).prune(keep_for=keep_for)

    async def _exchange(self, code: str, client: OAuthClient) -> str:
        """Trade the code for a user token, refusing one granted less than was asked for.

        GitHub answers 200 with an error in the body for a bad code rather than a 4xx, so
        checking the status alone reads a refusal as a success.

        Nor is a token proof of the scope it was asked for. GitHub documents that a person can
        grant less than was requested, and the answer then carries a token all the same, with what
        was really granted in `scope`. Issue #201. Every scope the client asked for has to be in
        it or nothing is kept, because a board token short of `project` would otherwise be stored
        and fail later, a long way from the one person who could fix it. Identity asks for
        nothing, so it can never be refused by this, and there is no purpose to look at.

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

        # Comma-separated, and anything that is not a string reads as nothing granted rather than
        # as something guessed at. One-line conditionals, so the coverage floor has only the
        # refusal itself to ask about.
        scope = payload.get("scope")
        granted: set[str] = (
            {one.strip() for one in scope.split(",")} if isinstance(scope, str) else set()
        )
        missing = {one.strip() for one in client.scope.split(",") if one.strip()} - granted
        if missing:
            # What is missing and nothing else. The token and the code are both credentials, and
            # a log line is the one place either could come to rest.
            logger.warning(
                "the oauth exchange came back without %s, so nothing was kept",
                ", ".join(sorted(missing)),
            )
            raise VerificationError(
                "GitHub granted less access than this bot asked for, so nothing was kept. Reading "
                "a board and moving its cards needs access to your projects. Run the command in "
                "Discord again and allow what GitHub asks for."
            )
        return token

    async def _discord_member(self, code: str) -> int:
        """Trade a Discord code for the id of the account that granted it.

        HTTP Basic for the client, which Discord accepts as readily as form fields and which keeps
        the secret out of a body. The token is used for one request and dropped. Discord's own
        reason is logged as its error code and nothing more: the rest is written for a developer,
        and it can carry the code back out.

        Unreachable is a refusal rather than a server error. The person has done everything right
        and the page in front of them should say what to do next, which is try again.
        """
        try:
            response = await self._http.post(
                DISCORD_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.discord_callback_url(),
                },
                auth=(self._discord.client_id, self._discord.client_secret),
                headers={"Accept": "application/json"},
            )
            payload = json_object(response)
            token = payload.get("access_token")
            if response.status_code >= 400 or not isinstance(token, str) or not token:
                logger.warning(
                    "the discord oauth exchange failed: %s", payload.get("error") or "no token"
                )
                raise VerificationError(DISCORD_REFUSED)
            me = json_object(
                await self._http.get(DISCORD_ME, headers={"Authorization": f"Bearer {token}"})
            )
        except httpx.HTTPError as unreachable:
            logger.warning(
                "discord could not be reached for a sign-in: %s", type(unreachable).__name__
            )
            raise VerificationError(DISCORD_REFUSED) from unreachable

        said = me.get("id")
        # A snowflake comes back as a string of digits. Held to ASCII digits before `int()`, which
        # would also accept surrounding whitespace and other scripts' digits.
        if not isinstance(said, str) or not (said.isascii() and said.isdigit()):
            raise VerificationError(DISCORD_SILENT)
        return int(said)

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


def _known_tiers(stored: frozenset[str]) -> frozenset[CommandRole]:
    """The stored tier names this code still knows, as tiers; anything else is dropped."""
    known = {role.value for role in CommandRole}
    return frozenset(CommandRole(name) for name in stored if name in known)


def _mac(browser: str, leg: str, state: str) -> str:
    """HMAC-SHA256 of one leg of one round trip, keyed by the browser's cookie, unpadded base64url.

    The shape is checked before the cookie becomes a key. A missing or blank cookie would otherwise
    be an empty key, and anybody can compute an HMAC under that.
    """
    if BROWSER_SHAPE.fullmatch(browser) is None:
        raise VerificationError(NOT_THIS_BROWSER)
    digest = hmac.new(browser.encode("ascii"), f"{leg}:{state}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _sealed(browser: str, leg: str, state: str) -> str:
    """A state only this browser can open again, for one leg of the round trip.

    One seal per leg, so a Discord seal is no use at GitHub's door and the other way round: each
    callback opens only the kind of seal it handed out.
    """
    return f"{state}.{_mac(browser, leg, state)}"


def _opened(browser: str, leg: str, sealed: str) -> str | None:
    """The state inside a seal, where this browser sealed it for this leg, or None.

    None for a cookie that is missing or malformed, before any key is made from it, and for a
    state no link could have, before it reaches the database. Compared as bytes in constant time:
    `compare_digest` raises on a `str` that is not ASCII, and the seal arrives from a query string
    anybody can write.
    """
    if BROWSER_SHAPE.fullmatch(browser) is None:
        return None
    state, dot, mac = sealed.rpartition(".")
    if not dot or STATE_SHAPE.fullmatch(state) is None:
        return None
    expected = _mac(browser, leg, state)
    return state if hmac.compare_digest(mac.encode(), expected.encode()) else None


def _binding(browser: str, state: str) -> str:
    """What a row holds for the browser that proved itself through Discord: hex, keyed per row.

    Keyed by the cookie over the state, so the database holds nothing that could be replayed - not
    the cookie, and not a hash of it that two rows would share and give away as the same browser.
    Only ever called with a cookie `_opened` has already accepted.
    """
    return hmac.new(browser.encode("ascii"), f"bound:{state}".encode(), hashlib.sha256).hexdigest()
