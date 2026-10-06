"""Pointing a repository at the project board its server mirrors.

Issue #158, requirement 1. The board was a pair of environment variables read once at boot, which
meant one board for the whole process and an operator with shell access to change it. Worse, it
meant the poller refused to run at all with two servers registered: nothing recorded which server
the board belonged to, so rather than guess it stopped.

The board is opened before it is stored, which is most of what is being tested here. A picker's
suggestions are only suggestions, so what arrives may be typed, may be a digit out, and may name
a board this token cannot see - and every one of those stored is a warning once a minute in a log
rather than a sentence read by the person who caused it.

Issue #201 added the rest: the board is opened and listed under the linker's OWN authorisation
rather than the App's, a board is told apart by whose account it is under rather than by how its
owner happened to be written down, and the picker's memory belongs to one member at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import BoardNotAuthorisedError, NotRegisteredError
from shannon.github.projects import ProjectListing
from shannon.services.board_credentials import BoardCredentials
from shannon.services.boards import (
    LIFETIME,
    BoardColumns,
    BoardLinkingService,
    BoardMovedError,
    BoardTakenError,
    BoardUnreadableError,
    OwnerBoards,
)
from tests.support import github_payloads as payloads
from tests.support.credentials import BOARD_KEY
from tests.support.db import blocked_on_a_row, register_repository

pytestmark = pytest.mark.integration

PROJECT = 3
# Whoever runs /board link. Their authorisation is what the board is read under
# from then on, so it is named rather than left as a bare literal. Issue #170.
LINKER = 555
# Somebody who has not. Only the refusal tests use them.
STRANGER = 999
# A second member who HAS authorised, for the tests about whose credential goes out: one
# authorised member cannot tell "theirs" apart from "whichever was found".
OTHER = 777


class FakeProjects:
    """An owner's boards, whether one of them opens, and whose credential each request carried."""

    def __init__(self, *numbers: int, opens: bool = True) -> None:
        self.boards = [ProjectListing(number=n, title=f"Board {n}") for n in numbers]
        self.opens = opens
        self.listed: list[str] = []
        self.opened: list[tuple[str, int]] = []
        # Every credential a request went out under, in order. Issue #201: the picker and the
        # check that a board exists used to send none at all, which the client fills in with the
        # App installation's token - so this is what the tests about that read.
        self.tokens: list[str] = []

    async def list_boards(self, owner: str, *, token: str) -> Sequence[ProjectListing]:
        self.listed.append(owner)
        self.tokens.append(token)
        return list(self.boards)

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        self.opened.append((owner, project_number))
        self.tokens.append(token)
        if not self.opens:
            return None
        return next((one for one in self.boards if one.number == project_number), None)


class MovedWhileOpening(FakeProjects):
    """A board that opens - and, while it is being opened, the repository's next delivery renames
    the row and commits, the way a transfer's first delivery would."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, to: str, owner_id: int
    ) -> None:
        super().__init__(PROJECT)
        self._sessionmaker = sessionmaker
        self._to = to
        self._owner_id = owner_id

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            repository = await repositories.get_by_guild(1)
            assert repository is not None
            await repositories.follow_rename(
                repository,
                repo_name=self._to,
                repo_url=f"https://github.com/{self._to}",
                owner_id=self._owner_id,
            )
        return await super().get_board(owner, project_number, token=token)


class MovingWhileOpening(FakeProjects):
    """A board that opens while the repository's next delivery is renaming the row: written and
    still uncommitted, so the row is held until the test lets it go."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, to: str, owner_id: int
    ) -> None:
        super().__init__(PROJECT)
        self._sessionmaker = sessionmaker
        self._to = to
        self._owner_id = owner_id
        self.held: AsyncSession | None = None

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        self.held = self._sessionmaker()
        await self.held.begin()
        repositories = RepositoryStore(self.held)
        repository = await repositories.get_by_guild(1)
        assert repository is not None
        await repositories.follow_rename(
            repository,
            repo_name=self._to,
            repo_url=f"https://github.com/{self._to}",
            owner_id=self._owner_id,
        )
        return await super().get_board(owner, project_number, token=token)


class WrittenWhileOpening(FakeProjects):
    """A board that opens - and, while it is being opened, another link commits a board of its
    own onto the same repository."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        number: int,
        owner: str | None,
        linked_by: int,
    ) -> None:
        super().__init__(PROJECT)
        self._sessionmaker = sessionmaker
        self._number = number
        self._owner = owner
        self._linked_by = linked_by

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None:
        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            repository = await repositories.get_by_guild(1)
            assert repository is not None
            await repositories.set_board(
                repository,
                project_number=self._number,
                project_owner=self._owner,
                linked_by=self._linked_by,
            )
        return await super().get_board(owner, project_number, token=token)


@pytest.fixture
def projects() -> FakeProjects:
    return FakeProjects(PROJECT, 77)


@pytest.fixture
def authorisations(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> BoardCredentials:
    return BoardCredentials(db_sessionmaker, keys=BOARD_KEY)


@pytest.fixture(autouse=True)
async def authorised(
    authorisations: BoardCredentials,
) -> None:
    """The linker has authorised, which every test in this file assumes but a few.

    Autouse, rather than a parameter on most of the tests. Since issue #170 linking refuses
    outright without an authorisation, so "whoever links has authorised" stopped being a thing a
    test sets up and became a precondition of linking a board at all. The tests that are about
    somebody who has not name `STRANGER` instead.
    """
    await authorisations.remember(
        guild_id=1,
        discord_user_id=LINKER,
        github_login="octocat",
        github_user_id=583231,
        token="gho_linker",
    )


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    projects: FakeProjects,
    authorisations: BoardCredentials,
) -> BoardLinkingService:
    return BoardLinkingService(
        db_sessionmaker, projects, OwnerBoards(projects, authorisations), authorisations
    )


async def stored(session: AsyncSession, repository_id: int) -> Repository:
    session.expire_all()
    found = await session.get(Repository, repository_id)
    assert found is not None
    return found


async def authorise(
    authorisations: BoardCredentials, *, guild_id: int, member: int, token: str
) -> None:
    """One more member's authorisation, in one server."""
    await authorisations.remember(
        guild_id=guild_id,
        discord_user_id=member,
        github_login=f"member{member}",
        github_user_id=member,
        token=token,
    )


def linking_with(
    projects: FakeProjects,
    sessionmaker: async_sessionmaker[AsyncSession],
    authorisations: BoardCredentials,
) -> BoardLinkingService:
    """The service, over a board reader one test needs to be its own."""
    return BoardLinkingService(
        sessionmaker, projects, OwnerBoards(projects, authorisations), authorisations
    )


async def a_second_server(session: AsyncSession, *, repo_name: str = "other/repo") -> None:
    """Another registered server, guild 2, whose repository belongs to another account."""
    await register_repository(
        session, guild_id=2, channel_id=500, github_repo_id=999, repo_name=repo_name
    )


class TestPointingAtABoard:
    async def test_it_is_written_to_the_repository(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        row = await stored(db_session, repository_id)
        assert row.project_number == PROJECT

    async def test_the_owner_is_left_null_when_it_is_the_repositorys_own(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """Null means "this repository's owner", which is what the poller already falls back to.
        Writing it out would freeze today's answer into the row and survive a rename."""
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        row = await stored(db_session, repository_id)
        assert row.project_number == PROJECT, (
            "no board was written at all, so the null below is the column's own default"
        )
        assert row.project_owner is None

    async def test_an_owner_somewhere_else_is_recorded(
        self,
        projects: FakeProjects,
        service: BoardLinkingService,
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme", acting=LINKER)

        assert (await stored(db_session, repository_id)).project_owner == "acme"
        assert projects.opened == [("acme", PROJECT)]

    async def test_the_reply_carries_whose_board_it_replaced(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """The same number under another account is another board, so the reply has to be able
        to say whose it was - see `said`."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        link = await service.assign(
            guild_id=1, project_number=PROJECT, typed_owner="acme", acting=LINKER
        )

        assert (link.replaced, link.replaced_owner) == (PROJECT, "Canon-Regularis")

    async def test_a_first_board_replaced_nobodys(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        link = await service.assign(
            guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        assert (link.replaced, link.replaced_owner) == (None, "")

    async def test_the_reply_carries_the_board_it_replaced(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        link = await service.assign(guild_id=1, project_number=77, typed_owner="", acting=LINKER)

        assert (link.number, link.replaced) == (77, PROJECT)


class TestTheBoardIsOpenedBeforeItIsStored:
    async def test_a_board_that_does_not_open_is_refused(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        projects.opens = False

        with pytest.raises(BoardUnreadableError, match="numbered 3"):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

    async def test_nothing_is_written_when_it_does_not_open(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        repository_id = registered.id
        projects.opens = False

        with pytest.raises(BoardUnreadableError):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert (await stored(db_session, repository_id)).project_number is None

    async def test_the_refusal_names_what_the_person_can_check(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """It used to name SHANNON_GITHUB_PROJECT_TOKEN, and since issue #170 there is no such
        thing to name: the credential is the runner's own authorisation, which they have already
        granted by the time they get here. So the refusal names the two halves of the address
        instead - the number and the owner - because a board number is a sequence GitHub keeps per
        account and a wrong owner answers exactly like a wrong number.
        """
        projects.opens = False

        with pytest.raises(BoardUnreadableError, match="number against the board's URL"):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

    async def test_it_is_opened_with_the_linkers_own_authorisation(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """Issue #201, and the reason a private board could not be linked at all. The check that
        it exists used to go out with no credential, which the client fills in with the App
        installation's token - and the App holds no Projects permission. The refusal then blamed
        "your GitHub authorisation", which had never been sent."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert projects.tokens == ["gho_linker"]

    async def test_it_is_opened_with_nobody_elses(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """With two members authorised, the one who ran it is the one whose credential goes out.
        One authorised member could not tell "theirs" apart from "whichever was found"."""
        await authorise(authorisations, guild_id=1, member=OTHER, token="gho_other")

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=OTHER)

        assert projects.tokens == ["gho_other"]


class TestOneBoardPerServer:
    async def test_a_board_another_server_has_is_refused(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """Two repositories on one board each mirror every draft card into their own server,
        because a tracked item is keyed by repository and nothing compares across them.

        Named by its owner in the second server, because that is the only way two servers CAN name
        one board: left blank, an owner is each repository's own, and those are two accounts.
        """
        await a_second_server(db_session)
        # Authorised in the second server too, and the fact that this line is needed is the
        # scoping rule working: an authorisation granted in one server is not one in another,
        # however much the same person holds both. Without it this test would stop at the
        # authorisation refusal and never reach the rule it is about.
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        with pytest.raises(BoardTakenError, match="Another server already mirrors"):
            await service.assign(
                guild_id=2, project_number=PROJECT, typed_owner="Canon-Regularis", acting=LINKER
            )

    async def test_the_refusal_names_no_other_server(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """Issue #201. The repository that has the board is another server's, so its name is not
        this server's to be told: naming it let anybody who could link a board list other
        servers' repositories - private ones included - one board number at a time."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        with pytest.raises(BoardTakenError) as refused:
            await service.assign(
                guild_id=2, project_number=PROJECT, typed_owner="Canon-Regularis", acting=LINKER
            )

        assert registered.repo_name.split("/")[1] not in refused.value.message
        assert "Canon-Regularis/" not in refused.value.message

    async def test_two_servers_each_on_their_own_accounts_first_board_are_both_fine(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """Issue #201. A board number is a sequence GitHub keeps per account, so every account's
        first board is #1 - and comparing the owner as stored read two servers' OWN boards of the
        same number as one board, refusing the second and telling it the first one's name."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        link = await service.assign(
            guild_id=2, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        assert (link.owner, link.number) == ("other", PROJECT)

    async def test_a_board_named_by_its_owner_is_the_same_board_left_blank_elsewhere(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """The other way round from the test above: one server names the board by its owner,
        and the server whose repository that owner holds then links its own board of the same
        number leaving the owner blank. Two spellings of one board, compared as one."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="other", acting=LINKER)

        with pytest.raises(BoardTakenError):
            await service.assign(guild_id=2, project_number=PROJECT, typed_owner="", acting=LINKER)

    async def test_an_owner_is_the_same_owner_whatever_its_case(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """GitHub's logins are case-insensitive, and the person typing one is not careful."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme", acting=LINKER)

        with pytest.raises(BoardTakenError):
            await service.assign(
                guild_id=2, project_number=PROJECT, typed_owner="ACME", acting=LINKER
            )

    async def test_a_board_that_will_not_open_is_never_reported_as_taken(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """The check runs after the board opened, so it only ever answers somebody who could see
        the board for themselves. Before that, "taken" would confirm a board somebody cannot open
        is in use elsewhere - which is not theirs to know either."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        projects.opens = False

        with pytest.raises(BoardUnreadableError):
            await service.assign(
                guild_id=2, project_number=PROJECT, typed_owner="Canon-Regularis", acting=LINKER
            )

    async def test_the_same_repository_setting_the_same_board_again_is_fine(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """It is its own board. Refusing here would make the command fail the second time
        somebody ran it with the same answer."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        link = await service.assign(
            guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        assert link.number == PROJECT


class TestTheRepositoryMovingWhileTheBoardOpens:
    """Found reviewing #201. The board is opened under the owner one read of the row named, and
    GitHub is asked with no lock held, so a transfer's first delivery can rename the row in
    between. Stored as the repository's own owner's, the board would then have been the NEW
    owner's board of that number, read under the linker's authorisation."""

    async def test_a_board_opened_under_the_owner_it_left_is_not_linked(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        repository_id = registered.id
        projects = MovedWhileOpening(db_sessionmaker, to="someone-else/Shannon-bot", owner_id=8)

        with pytest.raises(BoardMovedError, match="moved to someone-else/Shannon-bot"):
            await linking_with(projects, db_sessionmaker, authorisations).assign(
                guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
            )

        row = await stored(db_session, repository_id)
        assert row.repo_name == "someone-else/Shannon-bot", "the move itself was taken back"
        assert (row.project_number, row.project_linked_by) == (None, None)

    async def test_a_board_named_by_its_owner_is_linked_all_the_same(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """A named owner is nobody's repository, so the move cannot have changed which board it
        is."""
        repository_id = registered.id
        projects = MovedWhileOpening(db_sessionmaker, to="someone-else/Shannon-bot", owner_id=8)

        await linking_with(projects, db_sessionmaker, authorisations).assign(
            guild_id=1, project_number=PROJECT, typed_owner="boards-inc", acting=LINKER
        )

        row = await stored(db_session, repository_id)
        assert (row.project_number, row.project_owner) == (PROJECT, "boards-inc")

    async def test_a_rename_under_the_same_owner_is_linked_and_named_as_it_is_now(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
    ) -> None:
        """A board belongs to an account, and the account has not changed."""
        renamed = f"{payloads.OWNER}/renamed"
        projects = MovedWhileOpening(db_sessionmaker, to=renamed, owner_id=payloads.OWNER_ID)

        link = await linking_with(projects, db_sessionmaker, authorisations).assign(
            guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        assert (link.repo_name, link.number) == (renamed, PROJECT)

    async def test_a_move_still_in_flight_when_the_board_is_written_is_caught(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """Asked after the UPDATE rather than before it, because only the UPDATE waits for a move
        still in flight to commit. Asked before, the name would read as it was, and the link would
        land on the moved row."""
        repository_id = registered.id
        projects = MovingWhileOpening(db_sessionmaker, to="someone-else/Shannon-bot", owner_id=8)
        linking = asyncio.create_task(
            linking_with(projects, db_sessionmaker, authorisations).assign(
                guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
            )
        )
        try:
            await blocked_on_a_row(db_sessionmaker, linking)
            assert projects.held is not None
            await projects.held.commit()
        finally:
            if projects.held is not None:
                await projects.held.close()

        with pytest.raises(BoardMovedError, match="moved to someone-else/Shannon-bot"):
            await linking

        row = await stored(db_session, repository_id)
        assert row.repo_name == "someone-else/Shannon-bot"
        assert (row.project_number, row.project_linked_by) == (None, None)

    async def test_the_refusal_says_how_to_keep_the_board_that_was_meant(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
    ) -> None:
        """Running /board link again with the same bare number would link the NEW owner's board
        of that number after a transfer - so the way to the old one is named. After an account
        renamed itself the bare number is the right board, and the old login names nobody's, so
        that way is named as well."""
        projects = MovedWhileOpening(db_sessionmaker, to="someone-else/Shannon-bot", owner_id=8)

        with pytest.raises(BoardMovedError) as refused:
            await linking_with(projects, db_sessionmaker, authorisations).assign(
                guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
            )

        said = refused.value.message
        assert "naming Canon-Regularis as the owner if the repository was transferred" in said
        assert "by its number alone if Canon-Regularis only renamed itself someone-else" in said


class TestABoardChosenBeforeTheRepositoryMoved:
    """Found reviewing #201. A one-click link is followed up to ten minutes after the board was
    chosen, by a bare number meaning the repository's owner's board then. `chosen_under` is that
    owner, and a repository that has moved away from it since is refused before GitHub is asked."""

    async def test_a_bare_number_chosen_under_the_owner_it_left_is_refused_unopened(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        repository_id = registered.id

        with pytest.raises(BoardMovedError, match="has moved away from acme"):
            await service.assign(
                guild_id=1,
                project_number=PROJECT,
                typed_owner="",
                acting=LINKER,
                chosen_under="acme",
            )

        assert projects.opened == [], "the new owner's board was opened"
        assert (await stored(db_session, repository_id)).project_number is None

    async def test_the_owner_it_was_chosen_under_is_compared_without_its_case(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        link = await service.assign(
            guild_id=1,
            project_number=PROJECT,
            typed_owner="",
            acting=LINKER,
            chosen_under=payloads.OWNER.upper(),
        )

        assert link.number == PROJECT

    async def test_an_owner_somebody_named_is_not_second_guessed(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """A named owner is the board's whoever owns the repository, so there is nothing to have
        moved."""
        link = await service.assign(
            guild_id=1,
            project_number=PROJECT,
            typed_owner="boards-inc",
            acting=LINKER,
            chosen_under="acme",
        )

        assert link.owner == "boards-inc"


class TestABoardIsWrittenWhole:
    """Found reviewing #201. The ORM leaves a column out of the UPDATE where the new value equals
    the one the session read - and /board link reads the row without a lock, then asks GitHub. So a
    board another link committed in between kept whichever of its columns this link's read happened
    to agree with: a mixture neither link made, under a reply describing a third."""

    async def test_an_owner_another_link_wrote_meanwhile_does_not_survive(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        repository_id = registered.id
        projects = WrittenWhileOpening(
            db_sessionmaker, number=77, owner="boards-inc", linked_by=OTHER
        )

        await linking_with(projects, db_sessionmaker, authorisations).assign(
            guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        row = await stored(db_session, repository_id)
        assert (row.project_number, row.project_owner, row.project_linked_by) == (
            PROJECT,
            None,
            LINKER,
        )

    async def test_the_same_board_linked_again_is_written_again(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """Linking the board a row already has changes no column at all, so it used to write
        nothing - and a different board linked meanwhile stayed, under this link's reply."""
        repository_id = registered.id
        registered.project_number = PROJECT
        registered.project_linked_by = LINKER
        await db_session.commit()
        projects = WrittenWhileOpening(db_sessionmaker, number=77, owner=None, linked_by=OTHER)

        await linking_with(projects, db_sessionmaker, authorisations).assign(
            guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER
        )

        row = await stored(db_session, repository_id)
        assert (row.project_number, row.project_linked_by) == (PROJECT, LINKER)

    async def test_a_linker_a_move_took_off_is_put_back(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        authorisations: BoardCredentials,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """A move nothing can prove takes the board off its linker. Linked again, naming the old
        owner, while that move commits, the linker equals what the read saw - so it was left out,
        and a reply saying linked stood over a board nobody's authorisation stands behind."""
        repository_id = registered.id
        registered.github_owner_id = None
        registered.project_number = PROJECT
        registered.project_linked_by = LINKER
        await db_session.commit()
        projects = MovedWhileOpening(db_sessionmaker, to="someone-else/Shannon-bot", owner_id=8)

        await linking_with(projects, db_sessionmaker, authorisations).assign(
            guild_id=1, project_number=PROJECT, typed_owner=payloads.OWNER, acting=LINKER
        )

        row = await stored(db_session, repository_id)
        assert (row.project_owner, row.project_linked_by) == (payloads.OWNER, LINKER)


class TestUnlinking:
    async def test_both_columns_go(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """An owner with no number addresses nothing and would sit in the row looking like
        configuration."""
        repository_id = registered.id
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme", acting=LINKER)

        await service.unassign(guild_id=1)

        row = await stored(db_session, repository_id)
        assert (row.project_number, row.project_owner) == (None, None)

    async def test_it_says_what_was_dropped(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        unlinked = await service.unassign(guild_id=1)

        assert unlinked.replaced == PROJECT

    async def test_a_server_mirroring_nothing_says_so(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        unlinked = await service.unassign(guild_id=1)

        assert (unlinked.replaced, unlinked.forgot) == (None, None)

    async def test_unlinking_asks_github_nothing(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.unassign(guild_id=1)

        assert projects.opened == []

    async def test_it_names_whose_authorisation_went_with_it(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """The reply names them, because they are the only person who can also revoke it on
        GitHub - and the person who ran the command may well not be them. The docs said the reply
        did this, and until issue #201 it did not."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        unlinked = await service.unassign(guild_id=1)

        assert unlinked.forgot == LINKER

    async def test_an_unlink_that_fails_forgets_nobodys_authorisation(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The credential lives behind its own session, so it is forgotten only after the board
        is unlinked. The other order would leave a board still linked with nobody's
        authorisation behind it, whenever the unlink itself failed."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        async def breaks(self: RepositoryStore, *args: object, **kwargs: object) -> None:
            raise RuntimeError("the database went away")

        monkeypatch.setattr(RepositoryStore, "set_board", breaks)

        with pytest.raises(RuntimeError, match="went away"):
            await service.unassign(guild_id=1)

        assert await authorisations.granted_to(guild_id=1, discord_user_id=LINKER) is not None
        assert (await stored(db_session, registered.id)).project_number == PROJECT

    async def test_a_linker_who_had_already_withdrawn_is_not_named(
        self,
        service: BoardLinkingService,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """Nothing was forgotten, so nobody is told to go and revoke something on its account."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        await authorisations.forget(guild_id=1, discord_user_id=LINKER)

        unlinked = await service.unassign(guild_id=1)

        assert unlinked.forgot is None


class TestAServerWithNoRepository:
    async def test_linking_is_refused(self, service: BoardLinkingService) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await service.assign(
                guild_id=404, project_number=PROJECT, typed_owner="", acting=LINKER
            )

    async def test_unlinking_is_refused(self, service: BoardLinkingService) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await service.unassign(guild_id=404)

    async def test_asking_what_it_mirrors_is_refused(self, service: BoardLinkingService) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await service.standing(guild_id=404, asking=LINKER)

    async def test_the_picker_says_nothing_rather_than_raising(
        self, service: BoardLinkingService
    ) -> None:
        """An autocomplete has nowhere to put a refusal."""
        assert await service.choices_for(404, "", acting=LINKER) == ()


class TestWhatThePickerAsksFor:
    async def test_the_repositorys_own_owner_by_default(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.choices_for(1, "", acting=LINKER)

        assert projects.listed == ["Canon-Regularis"]

    async def test_a_typed_owner_instead(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.choices_for(1, "  acme  ", acting=LINKER)

        assert projects.listed == ["acme"]

    async def test_it_asks_github_once_across_keystrokes(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """Asked per keystroke, and Discord allows an autocomplete about three seconds."""
        await service.choices_for(1, "acme", acting=LINKER)
        await service.choices_for(1, "acme", acting=LINKER)

        assert projects.listed == ["acme"]

    async def test_it_answers_the_boards(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        found = await service.choices_for(1, "acme", acting=LINKER)

        assert [one.number for one in found] == [PROJECT, 77]

    async def test_it_lists_with_the_choosers_own_authorisation(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """Issue #201: the listing went out with no credential, which the client answers with the
        App installation's - so a private board was never offered to the person who could open
        it."""
        await service.choices_for(1, "acme", acting=LINKER)

        assert projects.tokens == ["gho_linker"]


class TestThePickerRemembersOneMemberAtATime:
    """Issue #201. A listing is made under the authorisation of whoever is choosing, so it holds
    the private boards their account can see. Remembered per owner alone, one member's private
    board titles were offered to the next person, in any server, who typed the same owner."""

    async def test_one_members_list_is_never_answered_to_another(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        await authorise(authorisations, guild_id=1, member=OTHER, token="gho_other")

        await service.choices_for(1, "acme", acting=LINKER)
        await service.choices_for(1, "acme", acting=OTHER)

        assert projects.tokens == ["gho_linker", "gho_other"], (
            "the second member was answered out of the first one's listing"
        )

    async def test_the_same_member_in_two_servers_is_asked_in_each(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """An authorisation is granted per server, so the same person is two askers."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=LINKER, token="gho_linker_there")

        await service.choices_for(1, "acme", acting=LINKER)
        await service.choices_for(2, "acme", acting=LINKER)

        assert projects.tokens == ["gho_linker", "gho_linker_there"]

    async def test_somebody_who_has_not_authorised_is_offered_nothing(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """And GitHub is not asked. A blank credential is not "anonymous" on this path: the client
        fills it in with the App installation's token, which is what issue #201 took off it."""
        assert await service.choices_for(1, "acme", acting=STRANGER) == ()
        assert projects.listed == []

    async def test_having_nothing_is_not_remembered(
        self,
        service: BoardLinkingService,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """Somebody who authorises a moment from now should see their boards on the next keystroke
        rather than two minutes later."""
        assert await service.choices_for(1, "acme", acting=STRANGER) == ()
        await authorise(authorisations, guild_id=1, member=STRANGER, token="gho_stranger")

        found = await service.choices_for(1, "acme", acting=STRANGER)

        assert [one.number for one in found] == [PROJECT, 77]

    async def test_a_listing_is_read_again_once_its_life_runs_out(
        self,
        projects: FakeProjects,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """Bounded staleness, the same bargain `RepositoryLabels` strikes: a board made on GitHub a
        moment ago can be picked."""
        clock = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        boards = OwnerBoards(projects, authorisations, now=lambda: clock)

        await boards.listed("acme", guild_id=1, member=LINKER)
        await boards.listed("acme", guild_id=1, member=LINKER)
        clock = clock + LIFETIME + timedelta(seconds=1)
        await boards.listed("acme", guild_id=1, member=LINKER)

        assert projects.listed == ["acme", "acme"]


class TestTheStoreUnderneath:
    async def test_only_repositories_with_a_board_are_listed(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        other = await register_repository(
            db_session, guild_id=2, channel_id=500, github_repo_id=999, repo_name="other/repo"
        )
        other.project_number = 77
        await db_session.commit()

        found = await RepositoryStore(db_session).with_boards()

        assert [row.repo_name for row in found] == ["other/repo"]

    async def test_a_board_left_blank_is_under_the_repositorys_own_owner(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """Issue #201. Null means "this repository's owner", and that is resolved in the query,
        where comparing as stored made a null match any account's board of the same number."""
        registered.project_number = PROJECT
        registered.project_owner = None
        await db_session.commit()

        store = RepositoryStore(db_session)
        assert [
            one.id for one in await store.mirroring(project_number=PROJECT, owner="Canon-Regularis")
        ] == [registered.id]
        assert await store.mirroring(project_number=PROJECT, owner="acme") == []

    async def test_the_owner_is_compared_without_its_case(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        registered.project_number = PROJECT
        registered.project_owner = "Acme"
        await db_session.commit()

        found = await RepositoryStore(db_session).mirroring(project_number=PROJECT, owner="aCME")

        assert [one.id for one in found] == [registered.id]

    async def test_a_named_owner_is_that_owner_and_not_the_repositorys(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        registered.project_number = PROJECT
        registered.project_owner = "acme"
        await db_session.commit()

        store = RepositoryStore(db_session)
        assert await store.mirroring(project_number=PROJECT, owner="Canon-Regularis") == []

    async def test_every_repository_on_one_board_is_answered(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """A pair linked before this check existed can still be in the table, and handing back
        whichever row came first would make the caller's decision for it."""
        other = await register_repository(
            db_session, guild_id=2, channel_id=500, github_repo_id=999, repo_name="other/repo"
        )
        registered.project_number = PROJECT
        other.project_number = PROJECT
        other.project_owner = "Canon-Regularis"
        await db_session.commit()

        found = await RepositoryStore(db_session).mirroring(
            project_number=PROJECT, owner="canon-regularis"
        )

        assert [one.id for one in found] == [registered.id, other.id]


class TestABoardIsReadAsItsOwnServersLinker:
    """Issue #201, and `BoardCredentials.reading` from the outside.

    A poll finds a board's credential by the board, and that lookup compared the owner as stored
    and then fell back to ANY row with the number and a null owner. So a server that named another
    server's board by its owner was found first, and its member's credential read the other
    server's board - a person's authorisation crossing between two servers, which is what keying
    the lookup on the board was for.
    """

    async def test_each_servers_own_first_board_is_read_as_its_own_linker(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=OTHER, token="gho_other")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        await service.assign(guild_id=2, project_number=PROJECT, typed_owner="", acting=OTHER)

        assert await authorisations.reading("Canon-Regularis", PROJECT) == "gho_linker"
        assert await authorisations.reading("other", PROJECT) == "gho_other"

    async def test_a_board_two_servers_claim_is_read_as_nobody(
        self,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """Rows from before the owner was resolved: one server's own #3, and another that named the
        same board by its owner. Choosing between them would be reading one server's board under
        the other server's member, so neither is read until one lets go."""
        await a_second_server(db_session)
        await authorise(authorisations, guild_id=2, member=OTHER, token="gho_other")
        store = RepositoryStore(db_session)
        second = await store.get_by_guild(2)
        assert second is not None
        await store.set_board(
            registered, project_number=PROJECT, project_owner=None, linked_by=LINKER
        )
        await store.set_board(
            second, project_number=PROJECT, project_owner="Canon-Regularis", linked_by=OTHER
        )
        await db_session.commit()

        assert await authorisations.reading("Canon-Regularis", PROJECT) == ""


class TestRelinkingForgetsTheOldBoardsCards:
    """A card id belongs to the board it is on.

    Pointed at a different board, a remembered id is not merely stale - it is wrong in a way
    that WRITES. It would be sent as a card id under the new board's owner and number, which is
    either a 404 nobody sees or, worse, another card entirely. Nothing else clears them: the
    poller only ever writes a pairing, and only for cards on the board it just read, so an item
    absent from the new board would keep the old id for ever.
    """

    async def carded(self, session: AsyncSession, registered: Repository) -> int:
        item = TrackedItem(
            repository_id=registered.id,
            github_object_id=4242,
            github_object_type=ObjectType.ISSUE,
            github_object_number=7,
            github_url="",
            title="An issue on the old board",
            project_item_id=999999,
        )
        session.add(item)
        await session.commit()
        return item.id

    async def stored(self, session: AsyncSession, item_id: int) -> TrackedItem:
        session.expire_all()
        found = await session.get(TrackedItem, item_id)
        assert found is not None
        return found

    async def test_pointing_at_another_board_forgets_them(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        item_id = await self.carded(db_session, registered)

        await service.assign(guild_id=1, project_number=77, typed_owner="", acting=LINKER)

        assert (await self.stored(db_session, item_id)).project_item_id is None

    async def test_the_same_number_under_another_owner_is_another_board(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        item_id = await self.carded(db_session, registered)

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme", acting=LINKER)

        assert (await self.stored(db_session, item_id)).project_item_id is None

    async def test_relinking_the_same_board_keeps_them(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """Issue #201. One click made "link the board this server already mirrors" the ordinary
        case - every re-authorisation is one - and forgetting there threw away every card pairing
        for a poll to rebuild, for nothing. Named in a different case, it is still the same
        board."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        item_id = await self.carded(db_session, registered)

        await service.assign(
            guild_id=1, project_number=PROJECT, typed_owner="canon-regularis", acting=LINKER
        )

        assert (await self.stored(db_session, item_id)).project_item_id == 999999

    async def test_unlinking_the_board_forgets_them_too(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        item_id = await self.carded(db_session, registered)

        await service.unassign(guild_id=1)

        assert (await self.stored(db_session, item_id)).project_item_id is None

    async def test_a_refused_link_forgets_nothing(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """The forget runs after every refusal, inside the same transaction, so a link that does
        not land leaves the row exactly as it was rather than half-applied."""
        item_id = await self.carded(db_session, registered)
        projects.opens = False

        with pytest.raises(BoardUnreadableError):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert (await self.stored(db_session, item_id)).project_item_id == 999999


class _CountingSessions:
    """A sessionmaker that records how many sessions were opened through it.

    The repository lookup is the half of this cache that a server with NO board still pays for,
    and it is invisible from the reader: with no board there is nothing to ask GitHub, so `asked`
    stays empty whether the answer was remembered or looked up again from scratch.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self.opened = 0

    def __call__(self) -> AsyncSession:
        self.opened += 1
        return self._sessionmaker()


class FakeColumnReader:
    """The Status columns of one board, out of a tuple. Records what it was asked."""

    def __init__(self, *columns: str) -> None:
        self.columns = columns
        self.asked: list[tuple[str, int]] = []

    async def status_columns(self, owner: str, project_number: int) -> tuple[str, ...]:
        self.asked.append((owner, project_number))
        return self.columns


class TestTheColumnsThePickerOffers:
    """What `/status` autocompletes over, resolved from a guild in the three seconds Discord allows.

    Keyed on the guild because that is what an autocomplete is handed. One cached answer per server
    holds the repository lookup and the GitHub read together, so a keystroke costs neither.
    """

    def columns(self, reader: FakeColumnReader, sessionmaker, **kwargs) -> BoardColumns:
        return BoardColumns(sessionmaker, reader, **kwargs)

    async def test_it_reads_the_board_this_server_mirrors(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await RepositoryStore(db_session).set_board(
            registered, project_number=6, project_owner=None
        )
        await db_session.commit()
        reader = FakeColumnReader("Backlog", "Ready", "Done")

        found = await self.columns(reader, db_sessionmaker).offered(registered.discord_guild_id)

        assert found == ("Backlog", "Ready", "Done")
        assert reader.asked == [("Canon-Regularis", 6)]

    async def test_a_board_somewhere_else_is_read_under_its_own_owner(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The board owner and the repository owner are kept apart everywhere else, and a picker
        reading the wrong account would offer columns from somebody else's board."""
        await RepositoryStore(db_session).set_board(
            registered, project_number=6, project_owner="acme"
        )
        await db_session.commit()
        reader = FakeColumnReader("Backlog")

        await self.columns(reader, db_sessionmaker).offered(registered.discord_guild_id)

        assert reader.asked == [("acme", 6)]

    async def test_a_server_mirroring_no_board_offers_nothing(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Nothing rather than a refusal. The picker puts this bot's own four names behind whatever
        comes back, so an empty answer is a picker that still works."""
        reader = FakeColumnReader("Backlog")

        found = await self.columns(reader, db_sessionmaker).offered(registered.discord_guild_id)

        assert found == ()
        assert reader.asked == [], "it asked GitHub about a board this server does not have"

    async def test_a_server_with_no_repository_offers_nothing(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        reader = FakeColumnReader("Backlog")

        assert await self.columns(reader, db_sessionmaker).offered(9999) == ()
        assert reader.asked == []

    async def test_a_second_keystroke_costs_nothing(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A picker reads this on every keystroke and Discord allows it about three seconds, so a
        repository lookup and a GitHub read per character is the cost this cache exists to avoid."""
        await RepositoryStore(db_session).set_board(
            registered, project_number=6, project_owner=None
        )
        await db_session.commit()
        reader = FakeColumnReader("Backlog")
        columns = self.columns(reader, db_sessionmaker)

        for _ in range(5):
            await columns.offered(registered.discord_guild_id)

        assert len(reader.asked) == 1

    async def test_having_no_board_is_remembered_too(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A server with no board is the common case for a bot in several, and a lookup per
        keystroke for an answer that is always nothing is the same cost worth avoiding.

        Counted, because nothing else here can see it. `()` is the answer with a cache and
        without one, and `FakeColumnReader` is never asked either way - there is no board to ask
        about - so the repository lookup is the only thing that shows the nothing was remembered.
        """
        sessions = _CountingSessions(db_sessionmaker)
        reader = FakeColumnReader()
        columns = self.columns(reader, sessions)
        guild = registered.discord_guild_id

        for _ in range(5):
            assert await columns.offered(guild) == ()

        assert sessions.opened == 1, "the repository was looked up again for an answer it had"
        assert reader.asked == [], "a board was read for a server that has none"

    async def test_a_column_renamed_on_the_board_arrives_once_the_life_runs_out(
        self,
        registered: Repository,
        db_session: AsyncSession,
        db_sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Bounded staleness rather than none. Long enough that typing a name is one call, short
        enough that a column renamed a moment ago can be picked."""
        await RepositoryStore(db_session).set_board(
            registered, project_number=6, project_owner=None
        )
        await db_session.commit()
        clock = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        reader = FakeColumnReader("Todo")
        columns = BoardColumns(db_sessionmaker, reader, now=lambda: clock)

        assert await columns.offered(registered.discord_guild_id) == ("Todo",)
        reader.columns = ("Backlog",)
        assert await columns.offered(registered.discord_guild_id) == ("Todo",), "it forgot too soon"

        clock = clock + LIFETIME + timedelta(seconds=1)
        assert await columns.offered(registered.discord_guild_id) == ("Backlog",)


class TestABoardIsLinkedBySomebodyWhoAuthorisedIt:
    """Issue #170. A board is read under the authorisation of whoever linked it, so linking one
    requires having authorised - and the row records who, because a poll has to find it again."""

    async def test_somebody_who_has_not_authorised_is_refused(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """The message names the command that fixes it, because this is the one board refusal the
        person reading it can fix themselves in one step."""
        with pytest.raises(BoardNotAuthorisedError, match="/board link"):
            await service.assign(
                guild_id=1, project_number=PROJECT, typed_owner="", acting=STRANGER
            )

    async def test_a_refused_link_writes_nothing_at_all(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        projects: FakeProjects,
    ) -> None:
        """Raised inside the same transaction as every other refusal, and before the board is
        opened, because opening it is what needs the credential."""
        with pytest.raises(BoardNotAuthorisedError):
            await service.assign(
                guild_id=1, project_number=PROJECT, typed_owner="", acting=STRANGER
            )

        assert (await stored(db_session, registered.id)).project_number is None
        assert projects.opened == [], "it asked GitHub about a board it had no credential for"

    async def test_the_member_who_linked_it_is_recorded(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """What a poll reads back to find the credential. Without it the board would be read under
        whichever authorisation happened to be found first, which is where this started."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert (await stored(db_session, registered.id)).project_linked_by == LINKER

    async def test_unlinking_the_board_forgets_the_authorisation(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """A credential kept for a board nobody mirrors is one nothing will ever use and nobody
        remembers granting, which is the worst kind to still hold."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        await service.unassign(guild_id=1)

        assert await authorisations.granted_to(guild_id=1, discord_user_id=LINKER) is None
        assert (await stored(db_session, registered.id)).project_linked_by is None

    async def test_unlinking_a_board_nobody_linked_forgets_nothing(
        self,
        service: BoardLinkingService,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """The other arm. A server with no board has nobody recorded against it, and reaching for
        a credential under a null member would be asking the store about user zero."""
        await service.unassign(guild_id=1)

        assert await authorisations.granted_to(guild_id=1, discord_user_id=LINKER) is not None

    async def test_relinking_keeps_the_linkers_authorisation(
        self,
        service: BoardLinkingService,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """Pointing at a different board is not withdrawing anything: the same person is still
        the one it is read as. Only unlinking lets the credential go."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        await service.assign(guild_id=1, project_number=77, typed_owner="", acting=LINKER)

        assert await authorisations.granted_to(guild_id=1, discord_user_id=LINKER) is not None

    async def test_somebody_else_relinking_it_takes_it_over_without_taking_anything(
        self,
        service: BoardLinkingService,
        registered: Repository,
        db_session: AsyncSession,
        authorisations: BoardCredentials,
    ) -> None:
        """The board is read as the new linker from then on, and the old one keeps their own
        authorisation: they may still be using it to move cards, and it was never this server's to
        throw away on their behalf."""
        await authorise(authorisations, guild_id=1, member=OTHER, token="gho_other")
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=OTHER)

        assert (await stored(db_session, registered.id)).project_linked_by == OTHER
        assert await authorisations.granted_to(guild_id=1, discord_user_id=LINKER) is not None


class TestWhatTheServerIsToldItMirrors:
    """`/board show`, and the one place somebody can find out why their board stopped mirroring."""

    async def test_a_server_mirroring_no_board(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        standing = await service.standing(guild_id=1, asking=LINKER)

        assert (standing.number, standing.title, standing.linked_by, standing.held) == (
            None,
            None,
            None,
            False,
        )
        assert projects.opened == []

    async def test_a_board_is_opened_as_the_poll_reads_it_and_not_as_the_asker(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """The question is whether the POLL can read it, and the poll reads it as the linker."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        projects.tokens.clear()

        standing = await service.standing(guild_id=1, asking=STRANGER)

        assert (standing.number, standing.title, standing.linked_by, standing.held) == (
            PROJECT,
            "Board 3",
            LINKER,
            True,
        )
        assert projects.tokens == ["gho_linker"]

    async def test_a_linker_who_withdrew_is_reported_and_github_is_not_asked(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        authorisations: BoardCredentials,
    ) -> None:
        """Never opened with no credential: the client would fill that in with the App's."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        await authorisations.forget(guild_id=1, discord_user_id=LINKER)
        projects.opened.clear()

        standing = await service.standing(guild_id=1, asking=LINKER)

        assert (standing.linked_by, standing.held, standing.title) == (LINKER, False, None)
        assert projects.opened == []

    async def test_a_board_that_will_not_open_has_no_title(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)
        projects.opens = False

        standing = await service.standing(guild_id=1, asking=LINKER)

        assert (standing.held, standing.title) == (True, None)

    async def test_a_board_linked_before_anybody_authorised_one(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """Every board that existed before issue #170: linked, and nobody recorded against it."""
        await RepositoryStore(db_session).set_board(
            registered, project_number=PROJECT, project_owner=None
        )
        await db_session.commit()

        standing = await service.standing(guild_id=1, asking=LINKER)

        assert (standing.number, standing.linked_by, standing.held) == (PROJECT, None, False)
        assert projects.opened == []

    async def test_a_board_two_servers_claim_is_reported_as_not_read(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """The poll reads such a board as nobody's, so reporting it as read under the linker's
        authorisation would be the one wrong answer this command could give - and it would
        open the board to say so, which the poll will not."""
        await a_second_server(db_session)
        store = RepositoryStore(db_session)
        second = await store.get_by_guild(2)
        assert second is not None
        await store.set_board(
            registered, project_number=PROJECT, project_owner=None, linked_by=LINKER
        )
        await store.set_board(
            second, project_number=PROJECT, project_owner="Canon-Regularis", linked_by=OTHER
        )
        await db_session.commit()

        standing = await service.standing(guild_id=1, asking=LINKER)

        assert (standing.shared, standing.title) == (True, None)
        assert projects.opened == [], "it opened a board the poll will not read"

    async def test_a_board_only_this_server_has_is_not_shared(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert (await service.standing(guild_id=1, asking=LINKER)).shared is False

    async def test_the_askers_own_authorisation_is_theirs_to_see(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        assert (await service.standing(guild_id=1, asking=LINKER)).yours == "octocat"
        assert (await service.standing(guild_id=1, asking=STRANGER)).yours is None

    async def test_the_owner_is_the_repositorys_own_where_none_is_named(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="", acting=LINKER)

        assert (await service.standing(guild_id=1, asking=LINKER)).owner == "Canon-Regularis"
