"""Pointing a repository at the project board mirrored into its server.

The board used to be a pair of environment variables read once at boot: one board for the whole
process, belonging to whichever repository happened to be the only one registered. That is what
made the poller refuse to run at all with two servers registered - nothing elected which one the
board belonged to, so rather than mirror one server's cards into one server's channels and say
nothing anywhere about the others, it stopped.

A board on the repository row answers the question the refusal was standing in for. What it
deliberately does not answer is a repository with SEVERAL boards: that needs a table, a rule for
two boards disagreeing about a status, and a cap to keep the reads inside one token's budget -
and GitHub's REST API cannot say which boards an issue is on in the first place.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.db.stores.tracked_items import TrackedItemStore
from shannon.domain.errors import NotRegisteredError, ShannonError
from shannon.github.projects import ProjectListing

# Long enough that choosing a board is one call rather than one per keystroke, short enough that
# a board made on GitHub a moment ago can be picked. The same bargain `RepositoryLabels` strikes,
# for the same reason: Discord allows an autocomplete about three seconds to answer.
LIFETIME = timedelta(minutes=2)


class BoardUnreadableError(ShannonError):
    """The board cannot be opened with the credential this deployment has."""


class BoardNotLinkedError(ShannonError):
    """This server mirrors no board, asked for something that only a board can answer.

    Distinct from `BoardUnreadableError`, because the two send somebody to different places: this
    one to `/set_board`, that one to the project token and the log. A caller that merely wants to
    know whether a board exists reads the row instead; this is for a caller that asked for the
    board's contents by name.
    """


class BoardTakenError(ShannonError):
    """Another registered repository is already mirroring this board."""


class ReadsProjects(Protocol):
    """Listing an owner's boards and opening one, which is all this needs of GitHub."""

    async def list_boards(self, owner: str) -> Sequence[ProjectListing]: ...

    async def get_board(self, owner: str, project_number: int) -> ProjectListing | None: ...


class ReadsColumns(Protocol):
    """The Status columns one board has, which is all the `/status` picker needs."""

    async def status_columns(self, owner: str, project_number: int) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class BoardLink:
    """What somebody who ran the command is told."""

    repo_name: str
    owner: str
    number: int
    title: str
    # The board this replaced, where it replaced one. A command that silently swapped a board
    # for another reads as having done nothing when the number was a digit out.
    replaced: int | None = None


@dataclass(frozen=True, slots=True)
class _Remembered:
    boards: tuple[ProjectListing, ...]
    until: datetime


class OwnerBoards:
    """Answers which boards an owner has, asking GitHub no more than it has to."""

    def __init__(
        self,
        projects: ReadsProjects,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = LIFETIME,
    ) -> None:
        self._projects = projects
        self._now = now
        self._lifetime = lifetime
        self._held: dict[str, _Remembered] = {}

    async def listed(self, owner: str) -> tuple[ProjectListing, ...]:
        key = owner.casefold()
        remembered = self._held.get(key)
        now = self._now()
        if remembered is not None and remembered.until > now:
            return remembered.boards

        found = tuple(await self._projects.list_boards(owner))
        self._held[key] = _Remembered(boards=found, until=now + self._lifetime)
        return found


class BoardLinkingService:
    """Backs `/set_board`: which board a server's repository mirrors."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        projects: ReadsProjects,
        boards: OwnerBoards,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._projects = projects
        self._boards = boards

    async def choices_for(self, guild_id: int, typed_owner: str) -> tuple[ProjectListing, ...]:
        """The boards to offer, under the owner asked for or the repository's own.

        Answers nothing rather than raising for a server with no repository: this feeds an
        autocomplete, which has nowhere to put a refusal.
        """
        owner = typed_owner.strip()
        if not owner:
            async with self._sessionmaker() as session:
                repository = await RepositoryStore(session).get_by_guild(guild_id)
                if repository is None:
                    return ()
                owner = repository.repo_name.partition("/")[0]
        return await self._boards.listed(owner)

    async def assign(
        self, *, guild_id: int, project_number: int | None, typed_owner: str
    ) -> BoardLink:
        """Point this server's repository at a board, or at none.

        The board is opened BEFORE it is stored. A picker's suggestions are only suggestions, so
        the number that arrives here may be typed, may be a digit out, and may name a board this
        token cannot see - and every one of those stored is a warning once a minute in a log
        rather than a sentence read by the person who caused it.
        """
        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            repository = await repositories.get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            replaced = repository.project_number
            # Before the branch rather than in each arm: one statement, no new arm to
            # cover, and both refusals below raise inside this same transaction, so a
            # refused /set_board still writes nothing at all.
            await TrackedItemStore(session).forget_the_board(repository.id)
            own_owner = repository.repo_name.partition("/")[0]
            if project_number is None:
                await repositories.set_board(repository, project_number=None, project_owner=None)
                return BoardLink(
                    repo_name=repository.repo_name,
                    owner=own_owner,
                    number=0,
                    title="",
                    replaced=replaced,
                )

            stored_owner = typed_owner.strip() or None
            owner = stored_owner or own_owner
            listing = await self._projects.get_board(owner, project_number)
            if listing is None:
                raise BoardUnreadableError(
                    f"{owner} has no project board numbered {project_number} that this bot can "
                    "open. Check the number in the board's URL, and check which kind of token "
                    "SHANNON_GITHUB_PROJECT_TOKEN holds: an organisation's board wants a "
                    "fine-grained one with Projects under organisation permissions, and a "
                    "personal board wants a classic one with read:project, because GitHub "
                    "publishes no Projects permission for a personal account at all. That "
                    "token is read once when this bot starts, so one added to .env since then "
                    "is not one it has yet."
                )

            taken = await repositories.linked_to_board(
                project_number=project_number, project_owner=stored_owner
            )
            if taken is not None and taken.id != repository.id:
                # Two repositories on one board each mirror every draft card into their own
                # server, because a tracked item is keyed by repository and nothing compares
                # across them. One query to refuse it is cheaper than the pair of threads.
                raise BoardTakenError(
                    f"{taken.repo_name} is already mirroring that board. A board belongs to one "
                    "repository here, because each would open its own thread for every card."
                )

            await repositories.set_board(
                repository, project_number=project_number, project_owner=stored_owner
            )
            return BoardLink(
                repo_name=repository.repo_name,
                owner=owner,
                number=listing.number,
                title=listing.title,
                replaced=replaced,
            )


@dataclass(frozen=True, slots=True)
class _RememberedColumns:
    columns: tuple[str, ...]
    until: datetime


class BoardColumns:
    """Which columns a server's board has, for the `/status` picker to offer.

    Keyed on the guild rather than on the board, because that is what the picker knows: an
    autocomplete is handed an interaction and has to get from there to a board in the three seconds
    Discord allows. One cached answer per server holds the repository lookup and the GitHub read
    together, so a keystroke costs neither.

    The same two-minute life `RepositoryLabels` uses, for the same reason: long enough that typing a
    name is one call, short enough that a column renamed on the board a moment ago can be picked.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        columns: ReadsColumns,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = LIFETIME,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._columns = columns
        self._now = now
        self._lifetime = lifetime
        self._held: dict[int, _RememberedColumns] = {}

    async def offered(self, guild_id: int) -> tuple[str, ...]:
        """The board's own columns, or nothing where this server mirrors no board.

        Nothing is the honest answer for a server with no board, no repository, or a board that
        will not read. The picker puts this bot's own four names behind whatever comes back, so
        an empty answer is a picker that still works rather than one that offers nothing.
        """
        remembered = self._held.get(guild_id)
        now = self._now()
        if remembered is not None and remembered.until > now:
            return remembered.columns

        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
        number = stored.project_number if stored is not None else None
        if stored is None or number is None:
            # Remembered all the same. A server with no board is the common case for a bot in
            # several, and a lookup per keystroke for an answer that is always nothing is the
            # cost this cache exists to avoid.
            self._held[guild_id] = _RememberedColumns(columns=(), until=now + self._lifetime)
            return ()

        owner = stored.project_owner or stored.repo_name.partition("/")[0]
        found = await self._columns.status_columns(owner, number)
        self._held[guild_id] = _RememberedColumns(columns=found, until=now + self._lifetime)
        return found
