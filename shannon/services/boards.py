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
from shannon.domain.board import board_owner
from shannon.domain.errors import (
    BoardNotAuthorisedError,
    NotRegisteredError,
    ShannonError,
)
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
    one to `/board link`, that one to `/board show` and the log. A caller that merely wants to
    know whether a board exists reads the row instead; this is for a caller that asked for the
    board's contents by name.
    """


class BoardTakenError(ShannonError):
    """Another registered repository is already mirroring this board."""


class ReadsProjects(Protocol):
    """Listing an owner's boards and opening one, which is all this needs of GitHub.

    Both as somebody in particular, and the credential is REQUIRED here although the reader
    underneath defaults it. Issue #201: with nothing passed, the client fills the credential in
    with the App installation's token - and the App holds no Projects permission, so the picker
    and the check that a board exists both ran as something that cannot see a private board, while
    the refusal blamed the person's own authorisation. Required at the seam, nothing can forget it.
    """

    async def list_boards(self, owner: str, *, token: str) -> Sequence[ProjectListing]: ...

    async def get_board(
        self, owner: str, project_number: int, *, token: str
    ) -> ProjectListing | None: ...


class ReadsColumns(Protocol):
    """The Status columns one board has, which is all the `/status` picker needs."""

    async def status_columns(self, owner: str, project_number: int) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class BoardLink:
    """What somebody who linked a board is told."""

    repo_name: str
    owner: str
    number: int
    title: str
    # The board this replaced, where it replaced one. A command that silently swapped a board
    # for another reads as having done nothing when the number was a digit out.
    replaced: int | None = None
    # Whose that board was, because the number alone does not say: a board is a number under
    # an account, and moving from one account's #3 to another's is a swap that the numbers
    # cannot show. Empty where nothing was replaced.
    replaced_owner: str = ""


@dataclass(frozen=True, slots=True)
class BoardUnlinked:
    """What somebody who unlinked a board is told."""

    repo_name: str
    # The board it stopped mirroring, or None where it was mirroring none.
    replaced: int | None
    # The member whose authorisation was forgotten with it, where there was one to forget. Named
    # in the reply because they are the only person who can also revoke it on GitHub, and the
    # person who ran the command may well not be them.
    forgot: int | None


@dataclass(frozen=True, slots=True)
class BoardStanding:
    """Which board a server mirrors and whose authorisation stands behind it, for `/board show`.

    Plain facts rather than a sentence, so the command decides how to say them - and so nothing
    here ever holds the linker's GitHub login, which is theirs: the reply names them by their
    Discord account, which the server already knows.
    """

    repo_name: str
    # None where it mirrors no board, and then the three after it say nothing.
    number: int | None
    owner: str
    # The board's own title where it opened, under the authorisation it is read with. None where
    # it would not open, or was not tried because nobody's authorisation stands behind it.
    title: str | None
    linked_by: int | None
    # Whether the authorisation it was linked with is still held. False with a linker named is a
    # member who withdrew, or one whose authorisation no longer decrypts.
    held: bool
    # The asker's own GitHub login, where they have authorised in this server. Theirs to see.
    yours: str | None
    # Whether another server's repository mirrors the same board too. Only a pair linked
    # before boards were told apart by whose they are can be in that state, and the poll reads
    # such a board as nobody's - so saying it was read would be the one wrong answer here.
    shared: bool = False


@dataclass(frozen=True, slots=True)
class _Remembered:
    boards: tuple[ProjectListing, ...]
    until: datetime


class HeldAuthorisation(Protocol):
    """As much of one person's authorisation as linking a board needs.

    The credential a board is opened with, and the account it belongs to - which is shown back to
    nobody but that person. Structural, so this module needs nothing from the one that decrypts.
    """

    @property
    def token(self) -> str: ...

    @property
    def github_login(self) -> str: ...


class HoldsBoardAuthorisations(Protocol):
    """The authorisations a board is reached with, as much of them as linking a board needs.

    No cipher, and nothing kept: this service asks whether somebody has authorised, hands their
    credential straight to the board reader - the way the workflow hands a mover's to a card
    write - and tells the store to let one go. It never stores a token or writes one to a log.
    Declared here because this is where it is consumed, which is the pattern the rest of the
    project already uses for a narrow handle.
    """

    async def granted_to(
        self, *, guild_id: int, discord_user_id: int
    ) -> HeldAuthorisation | None: ...

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool: ...


class OwnerBoards:
    """Answers which boards an owner has as one member can see them, asking GitHub no more than
    it has to.

    Per member, and that is the point of the key rather than a detail of it. Issue #201: a listing
    is made under the authorisation of whoever is choosing, so it includes the private boards
    their account can see - and a cache keyed on the owner alone would offer one member's private
    board titles to the next person, in any server, who typed the same owner. The server is in the
    key too, because an authorisation is granted per server.
    """

    def __init__(
        self,
        projects: ReadsProjects,
        authorisations: HoldsBoardAuthorisations,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = LIFETIME,
    ) -> None:
        self._projects = projects
        self._authorisations = authorisations
        self._now = now
        self._lifetime = lifetime
        self._held: dict[tuple[str, int, int], _Remembered] = {}

    async def listed(self, owner: str, *, guild_id: int, member: int) -> tuple[ProjectListing, ...]:
        key = (owner.casefold(), guild_id, member)
        remembered = self._held.get(key)
        now = self._now()
        if remembered is not None and remembered.until > now:
            return remembered.boards

        # On a miss only: a keystroke that is answered from memory costs no lookup and no
        # decryption either.
        granted = await self._authorisations.granted_to(guild_id=guild_id, discord_user_id=member)
        if granted is None:
            # Nothing, and GitHub is not asked. A blank credential is not "anonymous" on this
            # path: the client fills it in with the App installation's token, which is exactly
            # what issue #201 took off it. Not remembered either - it costs one lookup, and
            # somebody who authorises a moment from now should see their boards on the next
            # keystroke rather than two minutes later.
            return ()

        found = tuple(await self._projects.list_boards(owner, token=granted.token))
        # Expired entries go on the way in. Keyed per member, this would otherwise gain an entry
        # for everybody who ever opened the picker and never lose one.
        self._held = {kept: entry for kept, entry in self._held.items() if entry.until > now}
        self._held[key] = _Remembered(boards=found, until=now + self._lifetime)
        return found


class BoardLinkingService:
    """Backs `/board`: which board a server's repository mirrors, and whose authorisation
    reads it."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        projects: ReadsProjects,
        boards: OwnerBoards,
        authorisations: HoldsBoardAuthorisations,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._projects = projects
        self._boards = boards
        self._authorisations = authorisations

    async def choices_for(
        self, guild_id: int, typed_owner: str, *, acting: int
    ) -> tuple[ProjectListing, ...]:
        """The boards to offer, under the owner asked for or the repository's own.

        As the member choosing, which is what lets their private boards be offered at all - see
        `OwnerBoards`. Answers nothing rather than raising for a server with no repository: this
        feeds an autocomplete, which has nowhere to put a refusal.
        """
        owner = typed_owner.strip()
        if not owner:
            async with self._sessionmaker() as session:
                repository = await RepositoryStore(session).get_by_guild(guild_id)
                if repository is None:
                    return ()
                owner = repository.repo_name.partition("/")[0]
        return await self._boards.listed(owner, guild_id=guild_id, member=acting)

    async def assign(
        self, *, guild_id: int, project_number: int, typed_owner: str, acting: int
    ) -> BoardLink:
        """Point this server's repository at a board.

        The board is opened BEFORE it is stored. A picker's suggestions are only suggestions, so
        the number that arrives here may be typed, may be a digit out, and may name a board
        nobody authorised this bot to see - and every one of those stored is a warning in a log
        rather than a sentence read by the person who caused it.

        `acting` is whoever linked it, and since issue #170 that is not bookkeeping: their
        authorisation is what the board will be read under from here on, so it is recorded on the
        row and this refuses without one. Linking a board somebody else authorised would put
        this straight back where it started, with one person's credential serving a server.

        And the board is opened WITH that authorisation. Issue #201: this used to check only that
        one existed and then open the board with no credential at all, which the client fills in
        with the App installation's token - so a private board was refused with a sentence
        blaming an authorisation that was never sent.

        Every refusal raises inside the one transaction, so a refused link writes nothing at all.
        """
        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            repository = await repositories.get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            # Before the board is opened, because opening it is what needs the credential.
            granted = await self._authorisations.granted_to(
                guild_id=guild_id, discord_user_id=acting
            )
            if granted is None:
                raise BoardNotAuthorisedError(
                    "This bot has no GitHub authorisation from you, so it cannot check that board "
                    "exists or read it afterwards. Run /board link and sign in to GitHub when it "
                    "asks: a board is read as whoever links it, which is why it has to be you."
                )

            stored_owner = typed_owner.strip() or None
            owner = board_owner(project_owner=stored_owner, repo_name=repository.repo_name)
            listing = await self._projects.get_board(owner, project_number, token=granted.token)
            if listing is None:
                raise BoardUnreadableError(
                    f"{owner} has no project board numbered {project_number} that your GitHub "
                    "authorisation can open. Check the number against the board's URL, and the "
                    "owner against who owns it - a board number is a sequence GitHub keeps per "
                    "account, so the pair means something neither half does alone. If the board "
                    "belongs to somebody else, name its owner too, and your authorisation has to "
                    "cover it."
                )

            # After the board opened rather than before, so this only ever answers somebody who
            # can see the board for themselves - and it names nobody. The repository that has it
            # is another server's, so its name is not this server's to be told: a refusal that
            # named it would let anybody who can run this list other servers' repositories, one
            # board number at a time. Issue #201.
            others = [
                one
                for one in await repositories.mirroring(project_number=project_number, owner=owner)
                if one.id != repository.id
            ]
            if others:
                # Two repositories on one board each mirror every draft card into their own
                # server, because a tracked item is keyed by repository and nothing compares
                # across them. One query to refuse it is cheaper than the pair of threads.
                raise BoardTakenError(
                    "Another server already mirrors that board. A board belongs to one server "
                    "here, because each would open its own thread for every card."
                )

            replaced = repository.project_number
            was = board_owner(
                project_owner=repository.project_owner, repo_name=repository.repo_name
            )
            if replaced != project_number or was.lower() != owner.lower():
                # A card id and a column belong to the board they are on, and mean nothing - or
                # something wrong - on another; `forget_the_board` says why at length. Not on a
                # relink of the SAME board, though, which one click has made the ordinary case:
                # every re-authorisation is one, and forgetting there would throw away every card
                # pairing for a poll to rebuild, for nothing.
                #
                # Here rather than before the refusals above, which is where it used to be: it
                # updates every tracked item in the repository, and doing that first held those
                # row locks across the GitHub read in between.
                await TrackedItemStore(session).forget_the_board(repository.id)
            await repositories.set_board(
                repository,
                project_number=project_number,
                project_owner=stored_owner,
                linked_by=acting,
            )
            return BoardLink(
                repo_name=repository.repo_name,
                owner=owner,
                number=listing.number,
                title=listing.title,
                replaced=replaced,
                replaced_owner=was if replaced is not None else "",
            )

    async def unassign(self, *, guild_id: int) -> BoardUnlinked:
        """Stop mirroring this server's board, and let go of the authorisation it was read with.

        Whoever authorised it is told to let it go, not merely unpointed. A credential kept for a
        board this server no longer mirrors is a credential nothing will ever use and nobody
        remembers granting - which is the worst kind to still hold. GitHub's own grant stands until
        that person withdraws it there, which the reply says, naming them.

        Forgotten after the board is unlinked rather than inside the same transaction: the
        credential lives in its own table behind its own session, so "inside" would have meant
        committing the forget first - and a board left linked with nobody's authorisation behind
        it, had the unlink then failed.
        """
        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            repository = await repositories.get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")

            repo_name = repository.repo_name
            replaced = repository.project_number
            linked_by = repository.project_linked_by
            await TrackedItemStore(session).forget_the_board(repository.id)
            await repositories.set_board(repository, project_number=None, project_owner=None)

        # A server with no board has nobody recorded against it, and reaching for a credential
        # under a null member would be asking the store about user zero.
        forgot = linked_by is not None and await self._authorisations.forget(
            guild_id=guild_id, discord_user_id=linked_by
        )
        return BoardUnlinked(
            repo_name=repo_name, replaced=replaced, forgot=linked_by if forgot else None
        )

    async def standing(self, *, guild_id: int, asking: int) -> BoardStanding:
        """What this server mirrors, whose authorisation reads it, and whether that still works.

        The board is opened under the authorisation it is actually read with, never the asker's,
        because the question is whether the POLL can read it - and never with no credential,
        which the client would fill in with the App installation's. Somebody whose board has
        quietly stopped mirroring should be able to find out why from one command.
        """
        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_guild(guild_id)
            if repository is None:
                raise NotRegisteredError("This server has no repository yet. Run /register first.")
            repo_name = repository.repo_name
            number = repository.project_number
            linked_by = repository.project_linked_by
            owner = board_owner(project_owner=repository.project_owner, repo_name=repo_name)
            # The same question `reading` asks before the poll may read a board at all.
            claims = (
                ()
                if number is None
                else await RepositoryStore(session).mirroring(project_number=number, owner=owner)
            )
        shared = len(claims) > 1

        mine = await self._authorisations.granted_to(guild_id=guild_id, discord_user_id=asking)
        theirs = (
            None
            if linked_by is None
            else await self._authorisations.granted_to(guild_id=guild_id, discord_user_id=linked_by)
        )
        # Not opened where it is shared either: the poll will not read it, so whether it would
        # open is not the question.
        listing = (
            None
            if number is None or theirs is None or shared
            else await self._projects.get_board(owner, number, token=theirs.token)
        )
        return BoardStanding(
            repo_name=repo_name,
            number=number,
            owner=owner,
            title=listing.title if listing is not None else None,
            linked_by=linked_by,
            held=theirs is not None,
            yours=mine.github_login if mine is not None else None,
            shared=shared,
        )


def said(link: BoardLink) -> str:
    """What linking a board changed, in one sentence.

    One function for the reply in Discord and for the page a browser lands on after a one-click
    link, so the two cannot drift. A swapped board reads as having done nothing when the number was
    a digit out, so the old one is named rather than left for somebody to notice a week later -
    with its owner where that is what changed, because the same number under another account is
    another board, and `assign` has already forgotten every card pairing on that account.
    """
    if link.replaced is None:
        moved = ""
    elif link.replaced_owner and link.replaced_owner.lower() != link.owner.lower():
        moved = f" It was mirroring {link.replaced_owner}'s board #{link.replaced}."
    elif link.replaced != link.number:
        moved = f" It was mirroring #{link.replaced}."
    else:
        moved = ""
    return (
        f"{link.repo_name} now mirrors {link.owner}'s board #{link.number}, {link.title}.{moved} "
        "Cards appear at the next poll rather than at once."
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

        owner = board_owner(project_owner=stored.project_owner, repo_name=stored.repo_name)
        found = await self._columns.status_columns(owner, number)
        self._held[guild_id] = _RememberedColumns(columns=found, until=now + self._lifetime)
        return found
