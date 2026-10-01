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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, TrackedItem
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotRegisteredError
from shannon.github.projects import ProjectListing
from shannon.services.boards import (
    LIFETIME,
    BoardColumns,
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

        await service.assign(guild_id=1, project_number=77, typed_owner="")

        assert (await self.stored(db_session, item_id)).project_item_id is None

    async def test_clearing_the_board_forgets_them_too(
        self, service: BoardLinkingService, registered: Repository, db_session: AsyncSession
    ) -> None:
        item_id = await self.carded(db_session, registered)

        await service.assign(guild_id=1, project_number=None, typed_owner="")

        assert (await self.stored(db_session, item_id)).project_item_id is None

    async def test_a_refused_link_forgets_nothing(
        self,
        service: BoardLinkingService,
        projects: FakeProjects,
        registered: Repository,
        db_session: AsyncSession,
    ) -> None:
        """The clear runs before the refusals, inside the same transaction, so a /set_board that
        does not land leaves the row exactly as it was rather than half-applied."""
        item_id = await self.carded(db_session, registered)
        projects.opens = False

        with pytest.raises(BoardUnreadableError):
            await service.assign(guild_id=1, project_number=PROJECT, typed_owner="")

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
