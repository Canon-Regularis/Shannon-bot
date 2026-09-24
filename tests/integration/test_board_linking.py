"""Pointing a repository at the project board its server mirrors.

Issue #158, requirement 1. The board was a pair of environment variables read once at boot, which
meant one board for the whole process and an operator with shell access to change it. Worse, it
meant the poller refused to run at all with two servers registered: nothing recorded which server
the board belonged to, so rather than guess it stopped.

The board is opened before it is stored, which is most of what is being tested here. A picker's
suggestions are only suggestions, so what arrives may be typed, may be a digit out, and may name
a board this token cannot see - and every one of those stored is a warning once a minute in a log
rather than a sentence read by the person who caused it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.errors import NotRegisteredError
from shannon.github.projects import ProjectListing
from shannon.services.boards import (
    BoardLinkingService,
    BoardTakenError,
    BoardUnreadableError,
    OwnerBoards,
)
from tests.support.db import register_repository

pytestmark = pytest.mark.integration

PROJECT = 3


class FakeProjects:
    """An owner's boards, and whether one of them opens."""

    def __init__(self, *numbers: int, opens: bool = True) -> None:
        self.boards = [ProjectListing(number=n, title=f"Board {n}") for n in numbers]
        self.opens = opens
        self.listed: list[str] = []
        self.opened: list[tuple[str, int]] = []

    async def list_boards(self, owner: str) -> Sequence[ProjectListing]:
        self.listed.append(owner)
        return list(self.boards)

    async def get_board(self, owner: str, project_number: int) -> ProjectListing | None:
        self.opened.append((owner, project_number))
        if not self.opens:
            return None
        return next((one for one in self.boards if one.number == project_number), None)


@pytest.fixture
def projects() -> FakeProjects:
    return FakeProjects(PROJECT, 77)


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker[AsyncSession], projects: FakeProjects
) -> BoardLinkingService:
    return BoardLinkingService(db_sessionmaker, projects, OwnerBoards(projects))


async def stored(session: AsyncSession, repository_id: int) -> Repository:
    session.expire_all()
    found = await session.get(Repository, repository_id)
    assert found is not None
    return found


class TestPointingAtABoard:
    async def test_it_is_written_to_the_repository(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        row = await stored(db_session, repository_id)
        assert row.project_number == PROJECT

    async def test_the_owner_is_left_null_when_it_is_the_repositorys_own(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """Null means "this repository's owner", which is what the poller already falls back to.
        Writing it out would freeze today's answer into the row and survive a rename."""
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        assert (await stored(db_session, repository_id)).project_owner is None

    async def test_an_owner_somewhere_else_is_recorded(
        self,
        projects: FakeProjects,
        service: BoardLinkingService,
        db_session: AsyncSession,
        registered: Repository,
    ) -> None:
        repository_id = registered.id

        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme")

        assert (await stored(db_session, repository_id)).project_owner == "acme"
        assert projects.opened == [("acme", PROJECT)]

    async def test_the_reply_carries_the_board_it_replaced(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        link = await service.assign(guild_id=1, project_number=77, typed_owner="")

        assert (link.number, link.replaced) == (77, PROJECT)


class TestTheBoardIsOpenedBeforeItIsStored:
    async def test_a_board_that_does_not_open_is_refused(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        projects.opens = False

        with pytest.raises(BoardUnreadableError, match="numbered 3"):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

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
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        assert (await stored(db_session, repository_id)).project_number is None

    async def test_the_refusal_names_the_token(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """A board that does not open is as likely to be a token without Projects: Read-only for
        that owner as it is to be a wrong number, and the number is the one people check first."""
        projects.opens = False

        with pytest.raises(BoardUnreadableError, match="SHANNON_GITHUB_PROJECT_TOKEN"):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")


class TestOneBoardPerRepository:
    async def test_a_board_another_repository_has_is_refused(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """Two repositories on one board each mirror every draft card into their own server,
        because a tracked item is keyed by repository and nothing compares across them."""
        await register_repository(
            db_session, guild_id=2, channel_id=500, github_repo_id=999, repo_name="other/repo"
        )
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        with pytest.raises(BoardTakenError, match="already mirroring"):
            await service.assign(guild_id=2, project_number=PROJECT, typed_owner="")

    async def test_the_same_repository_setting_the_same_board_again_is_fine(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        """It is its own board. Refusing here would make the command fail the second time
        somebody ran it with the same answer."""
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        link = await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        assert link.number == PROJECT


class TestClearingIt:
    async def test_both_columns_go(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        """An owner with no number addresses nothing and would sit in the row looking like
        configuration."""
        repository_id = registered.id
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="acme")

        await service.assign(guild_id=1, project_number=None, typed_owner="")

        row = await stored(db_session, repository_id)
        assert (row.project_number, row.project_owner) == (None, None)

    async def test_it_says_what_was_dropped(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

        link = await service.assign(guild_id=1, project_number=None, typed_owner="")

        assert (link.number, link.replaced) == (0, PROJECT)

    async def test_clearing_asks_github_nothing(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.assign(guild_id=1, project_number=None, typed_owner="")

        assert projects.opened == []


class TestAServerWithNoRepository:
    async def test_assigning_is_refused(self, service: BoardLinkingService) -> None:
        with pytest.raises(NotRegisteredError, match="/register"):
            await service.assign(guild_id=404, project_number=PROJECT, typed_owner="")

    async def test_the_picker_says_nothing_rather_than_raising(
        self, service: BoardLinkingService
    ) -> None:
        """An autocomplete has nowhere to put a refusal."""
        assert await service.choices_for(404, "") == ()


class TestWhatThePickerAsksFor:
    async def test_the_repositorys_own_owner_by_default(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.choices_for(1, "")

        assert projects.listed == ["Canon-Regularis"]

    async def test_a_typed_owner_instead(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        await service.choices_for(1, "  acme  ")

        assert projects.listed == ["acme"]

    async def test_it_asks_github_once_across_keystrokes(
        self, service: BoardLinkingService, projects: FakeProjects, registered: Repository
    ) -> None:
        """Asked per keystroke, and Discord allows an autocomplete about three seconds."""
        await service.choices_for(1, "acme")
        await service.choices_for(1, "acme")

        assert projects.listed == ["acme"]

    async def test_it_answers_the_boards(
        self, service: BoardLinkingService, registered: Repository
    ) -> None:
        found = await service.choices_for(1, "acme")

        assert [one.number for one in found] == [PROJECT, 77]


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

    async def test_a_null_owner_and_a_named_one_are_different_boards(
        self, db_session: AsyncSession, registered: Repository
    ) -> None:
        """Accepted rather than resolved. Telling them apart would mean a GitHub call inside a
        uniqueness check, and the cost of being wrong is one refusal somebody can work around."""
        registered.project_number = PROJECT
        registered.project_owner = None
        await db_session.commit()

        store = RepositoryStore(db_session)
        assert await store.linked_to_board(project_number=PROJECT, project_owner=None) is not None
        assert await store.linked_to_board(project_number=PROJECT, project_owner="x") is None
