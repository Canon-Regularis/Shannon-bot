"""Following a board link authorises AND links the board. Issue #201.

`/board link` with no authorisation behind it used to be refused, and the person sent off to a
second command, to GitHub, and back to run the first one again. Now the link it hands out remembers
which board was chosen, and following it is the whole of it - the shape `/link` already had.

Driven against a real database, the real verification service, the real board-linking service and
the real OAuth routes, because what makes this safe is not any one of them. It is that the board
rides on the server-side row rather than in the URL, that it is linked as the member the row names
and with the authorisation GitHub has just granted, and that a link which cannot be finished still
keeps that authorisation and says why.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.api.routes import oauth
from shannon.commands._permissions import BOARD_ROLES, REGISTER_ROLES
from shannon.db.models import Repository
from shannon.db.stores.identities import IdentityVerificationStore
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.roles import CommandRole
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose
from shannon.github.projects import ProjectListing
from shannon.services.board_credentials import BoardCredentials
from shannon.services.boards import BoardLinkingService, OwnerBoards
from shannon.services.linking import UserLinkingService
from shannon.services.verification import (
    BOARD_SCOPE,
    LINK_LIFETIME,
    BoardLinked,
    BoardNotLinked,
    GitHubIdentityVerification,
    OAuthClient,
)
from tests.fakes.tiers import FakeTiers
from tests.support.credentials import BOARD_KEY
from tests.support.db import register_repository
from tests.support.round_trip import (
    DISCORD_APP,
    browser_on,
    discord_says,
    followed_in,
    round_trip,
    state_of,
    to_github_in,
)

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
PROJECT = 3
# A link written straight into the table, shaped like one this bot mints.
UNREAD = "u" * 43
# The board most of these links are handed out for. A constant rather than a call in an
# argument default, which ruff refuses on the general grounds that most such defaults are
# mutable.
THE_BOARD = ChosenBoard(number=PROJECT)
# The two halves of `/board` that hand out a link, as the board and the tier each sends with it.
BOTH_HALVES = [(THE_BOARD, REGISTER_ROLES), (None, BOARD_ROLES)]


def github_says(*, scope: str = "project", login: str = "octocat"):
    """A GitHub that completes the round trip, grants `scope`, and names one account."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            body = {"access_token": "gho_granted", "scope": scope, "token_type": "bearer"}
            return httpx.Response(200, content=json.dumps(body))
        return httpx.Response(200, content=json.dumps({"login": login, "id": 583231}))

    return handler


class FakeProjects:
    """An owner's boards, whether they open, and whose credential each request carried."""

    def __init__(self, *boards: ProjectListing, opens: bool = True) -> None:
        self.boards = list(boards) or [ProjectListing(number=PROJECT, title="Roadmap")]
        self.opens = opens
        self.tokens: list[str] = []
        self.breaks: Exception | None = None

    async def list_boards(self, owner: str, *, token: str) -> Sequence[ProjectListing]:
        self.tokens.append(token)
        return list(self.boards)

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        self.tokens.append(token)
        if self.breaks is not None:
            raise self.breaks
        if not self.opens:
            return None
        return next((one for one in self.boards if one.number == project_number), None)


@asynccontextmanager
async def verifying(
    sessionmaker: async_sessionmaker[AsyncSession],
    projects: FakeProjects,
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    keys: str = BOARD_KEY,
    tiers: FakeTiers | None = None,
) -> AsyncIterator[GitHubIdentityVerification]:
    """The real services, end to end, over a GitHub that is a function."""
    credentials = BoardCredentials(sessionmaker, keys=keys)
    linking = BoardLinkingService(
        sessionmaker, projects, OwnerBoards(projects, credentials), credentials
    )
    transport = httpx.MockTransport(discord_says(handler or github_says()))
    async with httpx.AsyncClient(transport=transport) as http:
        yield GitHubIdentityVerification(
            sessionmaker,
            UserLinkingService(sessionmaker),
            credentials,
            board_links=linking,
            tiers=tiers or FakeTiers(),
            client_id="Iv23liAbC",
            client_secret="shh",
            oauth_url="https://github.com",
            public_base_url="https://shannon.example.com",
            http=http,
            board=OAuthClient(
                client_id="Ov23liBoard", client_secret="board-shh", scope=BOARD_SCOPE
            ),
            discord=DISCORD_APP,
            now=lambda: NOW,
        )


async def follow(
    verification: GitHubIdentityVerification,
    *,
    board: ChosenBoard | None = THE_BOARD,
    guild_id: int = GUILD,
    tier: frozenset[CommandRole] = REGISTER_ROLES,
) -> httpx.Response:
    """Hand out a board link for Alice, carrying `board`, and have her browser follow it.

    Gated on the tier `/board link` uses unless a test says otherwise, because that is the half
    that hands out a link carrying a board.
    """
    url = await verification.link_for(
        guild_id=guild_id,
        discord_user_id=ALICE,
        purpose=VerificationPurpose.BOARD,
        board=board,
        tier=tier,
    )
    async with browser_on(verification) as client:
        return await followed_in(client, url, member=ALICE)


async def kept(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Whether Alice's authorisation is held, which every outcome but a refused sign-in keeps."""
    credentials = BoardCredentials(sessionmaker, keys=BOARD_KEY)
    return await credentials.granted_to(guild_id=GUILD, discord_user_id=ALICE) is not None


async def stored(session: AsyncSession, repository_id: int) -> Repository:
    session.expire_all()
    found = await session.get(Repository, repository_id)
    assert found is not None
    return found


class TestFollowingTheLinkLinksTheBoard:
    async def test_the_board_is_mirrored_and_nothing_else_is_run(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            response = await follow(verification)

        row = await stored(db_session, registered.id)
        assert (row.project_number, row.project_linked_by) == (PROJECT, ALICE)
        assert response.status_code == 200
        assert "now mirrors Canon-Regularis's board #3, Roadmap." in response.text
        assert "nothing else to run" in response.text

    async def test_the_authorisation_is_kept_as_well(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
    ) -> None:
        """It is what the board is read under from now on, so a link without it would be a board
        nobody can read."""
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            await follow(verification)

        credentials = BoardCredentials(db_sessionmaker, keys=BOARD_KEY)
        assert await credentials.reading("Canon-Regularis", PROJECT) == "gho_granted"

    async def test_the_board_is_opened_with_the_authorisation_just_granted(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """The check that the board exists goes out as the person who just signed in - the
        credential the board will be read under - and never as the App."""
        projects = FakeProjects()

        async with verifying(db_sessionmaker, projects) as verification:
            await follow(verification)

        assert projects.tokens == ["gho_granted"]

    async def test_the_page_names_the_account_that_signed_in(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            response = await follow(verification)

        assert response.text.startswith("Signed in as octocat.")

    async def test_a_board_named_by_its_owner_is_linked_under_that_owner(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            response = await follow(verification, board=ChosenBoard(number=PROJECT, owner="acme"))

        assert (await stored(db_session, registered.id)).project_owner == "acme"
        assert "acme's board #3" in response.text

    async def test_a_title_with_braces_is_shown_as_written(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """A board's title is somebody's free text, and this page is a template. A brace in it
        read as a placeholder would fail the page after the board had already linked."""
        projects = FakeProjects(ProjectListing(number=PROJECT, title="Q3 {login} {0}"))

        async with verifying(db_sessionmaker, projects) as verification:
            response = await follow(verification)

        assert response.status_code == 200
        assert "Q3 {login} {0}" in response.text


class TestALinkThatCannotBeFinished:
    """The person did authorise, and that stands: it is what `/status` moves their cards with and
    what the next `/board link` opens the board with. So the page says what happened to the board,
    keeps the grant, and answers 200 - GitHub has taken the code by now, and a server error for a
    sign-in that worked would be the wrong thing to show anybody."""

    async def test_a_board_that_will_not_open(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects(opens=False)) as verification:
            response = await follow(verification)

        assert response.status_code == 200
        assert "the board was not linked" in response.text
        assert "numbered 3" in response.text
        assert (await stored(db_session, registered.id)).project_number is None
        assert await kept(db_sessionmaker)

    async def test_a_board_another_server_already_mirrors_names_nobody(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        """This page is on the open internet. The repository that has the board is another
        server's, and its name is nobody's business here."""
        other = await register_repository(
            db_session, guild_id=2, channel_id=500, github_repo_id=999, repo_name="secret/thing"
        )
        other.project_number = PROJECT
        other.project_owner = "Canon-Regularis"
        await db_session.commit()

        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            response = await follow(verification)

        assert response.status_code == 200
        assert "Another server already mirrors that board" in response.text
        assert "secret" not in response.text
        assert (await stored(db_session, registered.id)).project_number is None
        assert await kept(db_sessionmaker)

    async def test_a_server_with_no_repository(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            response = await follow(verification)

        assert response.status_code == 200
        assert "/register" in response.text
        assert await kept(db_sessionmaker)

    async def test_anything_else_is_logged_and_still_answered(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The state is spent and the grant is kept, so this cannot be a bare 500. The operator
        gets the traceback; the person gets a sentence that is true."""
        projects = FakeProjects()
        projects.breaks = RuntimeError("the database went away")

        with caplog.at_level("ERROR", logger="shannon.services.verification"):
            async with verifying(db_sessionmaker, projects) as verification:
                response = await follow(verification)

        assert response.status_code == 200
        assert "the board was not linked" in response.text
        assert "the database went away" not in response.text, "an internal error reached a page"
        assert "linking it then failed" in caplog.text
        assert (await stored(db_session, registered.id)).project_number is None
        assert await kept(db_sessionmaker)

    async def test_too_little_scope_links_nothing_and_keeps_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        """Granted less than it asked for, the sign-in is refused before anything is kept - and
        so before anything is linked, because linking opens the board with what was kept."""
        handler = github_says(scope="read:project")

        async with verifying(db_sessionmaker, FakeProjects(), handler=handler) as verification:
            response = await follow(verification)

        assert response.status_code == 400
        assert (await stored(db_session, registered.id)).project_number is None
        assert not await kept(db_sessionmaker), "a short grant was kept"

    async def test_a_deployment_that_cannot_keep_it_links_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects(), keys="") as verification:
            response = await follow(verification)

        assert response.status_code == 400
        assert (await stored(db_session, registered.id)).project_number is None


class TestWhatALinkWithoutABoardDoes:
    async def test_an_authorise_only_link_links_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        """`/board authorise` changes nothing about what the server mirrors."""
        projects = FakeProjects()

        async with verifying(db_sessionmaker, projects) as verification:
            response = await follow(verification, board=None)

        assert response.status_code == 200
        assert (await stored(db_session, registered.id)).project_number is None
        assert projects.tokens == [], "a board was opened for a link that carried none"
        assert "/board withdraw" in response.text

    async def test_an_identity_link_never_links_a_board(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        projects = FakeProjects()

        async with verifying(db_sessionmaker, projects) as verification:
            url = await verification.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
            )
            verified = await round_trip(verification, url, member=ALICE)

        assert verified.board is None
        assert projects.tokens == []
        assert (await stored(db_session, registered.id)).project_number is None


class TestFollowingALinkMoreThanOnce:
    async def test_a_link_followed_twice_links_once(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        projects = FakeProjects()

        async with verifying(db_sessionmaker, projects) as verification:
            url = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=PROJECT),
            )
            async with browser_on(verification) as client:
                github = await to_github_in(client, url, member=ALICE)
                callback = {"code": "abc", "state": state_of(github)}
                first = await client.get("/oauth/github/callback", params=callback)
                second = await client.get("/oauth/github/callback", params=callback)

        assert (first.status_code, second.status_code) == (200, 400)
        assert projects.tokens == ["gho_granted"], "the board was linked twice"

    async def test_two_pending_links_each_link_their_own_board_and_the_last_click_wins(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        """Accepted and written down rather than prevented: both links are the same person's, in
        the same server, and each is good for ten minutes."""
        projects = FakeProjects(
            ProjectListing(number=PROJECT, title="Roadmap"), ProjectListing(number=77, title="Bugs")
        )

        async with verifying(db_sessionmaker, projects) as verification:
            to_three = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=PROJECT),
            )
            to_seventy_seven = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=77),
            )
            # In one browser, so one cookie carries both: the second link reuses the cookie the
            # first one left rather than knocking it over.
            async with browser_on(verification) as client:
                pages = [
                    await followed_in(client, url, member=ALICE)
                    for url in (to_seventy_seven, to_three)
                ]

        # Each link linked the board it was handed out for, in the order they were followed.
        assert "board #77, Bugs" in pages[0].text
        assert "board #3, Roadmap" in pages[1].text
        assert (await stored(db_session, registered.id)).project_number == PROJECT


class TestTheOutcomeTheRouteIsHanded:
    """`Verified.board`, read directly, because the pages are built from it and a page is the
    wrong place to find out the outcome was mislabelled."""

    async def test_a_linked_board_is_handed_back_as_linked(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects()) as verification:
            url = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=PROJECT),
            )
            verified = await round_trip(verification, url, member=ALICE)

        assert isinstance(verified.board, BoardLinked)
        assert verified.board.link.number == PROJECT

    async def test_a_refused_board_is_handed_back_with_its_reason(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        async with verifying(db_sessionmaker, FakeProjects(opens=False)) as verification:
            url = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=ChosenBoard(number=PROJECT),
            )
            verified = await round_trip(verification, url, member=ALICE)

        assert isinstance(verified.board, BoardNotLinked)
        assert "numbered 3" in verified.board.reason


class TestTheRoleIsAskedForAgainWhenTheLinkIsFollowed:
    """Found reviewing #201. The role was checked when the command ran, and the link can be
    followed up to ten minutes later - from a browser, with no interaction to read roles off. So
    following a board link asks Discord again, before anything is kept or linked."""

    @pytest.mark.parametrize(("board", "tier"), BOTH_HALVES, ids=["link", "authorise"])
    async def test_a_role_lost_in_between_keeps_nothing_and_links_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
        board: ChosenBoard | None,
        tier: frozenset[CommandRole],
    ) -> None:
        github = github_says()
        asked: list[httpx.Request] = []

        def recorded(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            return github(request)

        async with verifying(
            db_sessionmaker, FakeProjects(), handler=recorded, tiers=FakeTiers(held=False)
        ) as verification:
            response = await follow(verification, board=board, tier=tier)

        assert response.status_code == 400
        assert "no longer hold a role" in response.text
        assert asked == [], "GitHub was asked to spend a code for somebody no longer allowed"
        assert not await kept(db_sessionmaker)
        assert (await stored(db_session, registered.id)).project_number is None

    async def test_the_refusal_uses_the_link_up(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Running the command again asks for the role the ordinary way, which is the right place
        for the answer to come from."""
        async with verifying(
            db_sessionmaker, FakeProjects(), tiers=FakeTiers(held=False)
        ) as verification:
            url = await verification.link_for(
                guild_id=GUILD,
                discord_user_id=ALICE,
                purpose=VerificationPurpose.BOARD,
                board=THE_BOARD,
                tier=REGISTER_ROLES,
            )
            async with browser_on(verification) as client:
                github = await to_github_in(client, url, member=ALICE)
                callback = {"code": "abc", "state": state_of(github)}
                first = await client.get("/oauth/github/callback", params=callback)
                second = await client.get("/oauth/github/callback", params=callback)

        assert "no longer hold a role" in first.text
        assert "expired or has already been used" in second.text

    @pytest.mark.parametrize(("board", "tier"), BOTH_HALVES, ids=["link", "authorise"])
    async def test_discord_not_answering_keeps_nothing_either(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
        board: ChosenBoard | None,
        tier: frozenset[CommandRole],
    ) -> None:
        """Failing closed: could not check is not allowed."""
        tiers = FakeTiers(fails=DiscordGatewayError("Discord is down"))
        github = github_says()
        asked: list[httpx.Request] = []

        def recorded(request: httpx.Request) -> httpx.Response:
            asked.append(request)
            return github(request)

        async with verifying(
            db_sessionmaker, FakeProjects(), handler=recorded, tiers=tiers
        ) as verification:
            response = await follow(verification, board=board, tier=tier)

        assert response.status_code == 400
        assert "Discord could not be asked" in response.text
        assert asked == [], "GitHub was asked to spend a code nobody could vouch for"
        assert not await kept(db_sessionmaker)
        assert (await stored(db_session, registered.id)).project_number is None

    @pytest.mark.parametrize(("board", "tier"), BOTH_HALVES, ids=["link", "authorise"])
    async def test_it_asks_about_the_tier_the_link_was_handed_out_under(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        board: ChosenBoard | None,
        tier: frozenset[CommandRole],
    ) -> None:
        tiers = FakeTiers()

        async with verifying(db_sessionmaker, FakeProjects(), tiers=tiers) as verification:
            response = await follow(verification, board=board, tier=tier)

        assert response.status_code == 200
        assert tiers.asked == [(GUILD, ALICE, tier)]

    async def test_an_identity_link_never_asks(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A `/register` or `/unregister` link records a proof and nothing more, and running the
        command again asks for the role again; a `/link` link binds the account, but linking
        yourself takes no role. So a member with no role at all still finishes any of them."""
        tiers = FakeTiers(held=False)

        async with verifying(db_sessionmaker, FakeProjects(), tiers=tiers) as verification:
            for purpose in (
                VerificationPurpose.LINK,
                VerificationPurpose.REGISTER,
                VerificationPurpose.UNREGISTER,
            ):
                url = await verification.link_for(
                    guild_id=GUILD, discord_user_id=ALICE, purpose=purpose
                )
                await round_trip(verification, url, member=ALICE)

        assert tiers.asked == []

    @pytest.mark.parametrize(
        ("written", "asked"),
        [
            (frozenset({"PROJECT_MANAGER", "TEA_MAKER"}), frozenset({CommandRole.PROJECT_MANAGER})),
            (frozenset({"TEA_MAKER"}), frozenset()),
            (None, frozenset()),
        ],
        ids=["one-known-one-not", "none-known", "from-before-the-column"],
    )
    async def test_a_tier_this_code_cannot_read_narrows_the_question(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        registered: Repository,
        written: frozenset[str] | None,
        asked: frozenset[CommandRole],
    ) -> None:
        """A name this code no longer knows is dropped rather than guessed at, which narrows the
        question towards administrators - who hold every tier - and a board link from before the
        tier was written down is asked about as administrators only."""
        await IdentityVerificationStore(db_session).issue(
            state=UNREAD,
            guild_id=GUILD,
            discord_user_id=ALICE,
            purpose=VerificationPurpose.BOARD,
            lifetime=LINK_LIFETIME,
            board=THE_BOARD,
            tier=written,
        )
        await db_session.commit()
        tiers = FakeTiers()

        async with verifying(db_sessionmaker, FakeProjects(), tiers=tiers) as verification:
            await round_trip(
                verification,
                f"https://shannon.example.com/oauth/start?state={UNREAD}",
                member=ALICE,
            )

        assert tiers.asked == [(GUILD, ALICE, asked)]


class TestThePages:
    def test_every_page_names_the_account_that_signed_in(self) -> None:
        """Over every page rather than a list, so one added later is enrolled by existing."""
        pages = [*oauth.FINISHED.values(), oauth.LINKED, oauth.NOT_LINKED]

        for page in pages:
            filled = page.format(login="octocat", said="It linked.", reason="It did not.")
            assert filled.startswith("Signed in as octocat.")

    def test_the_board_page_says_how_to_take_it_back(self) -> None:
        """Not `/set_board`, which it said for a release after that stopped being how anything
        was undone."""
        said = oauth.FINISHED[VerificationPurpose.BOARD]

        assert "/board withdraw" in said
        assert "/set_board" not in said
