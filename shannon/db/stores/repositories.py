from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from shannon.db.models import Repository

logger = logging.getLogger(__name__)


class RepositoryStore:
    """Data access for registered GitHub repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_guild(self, discord_guild_id: int) -> Repository | None:
        found: Repository | None = await self._session.scalar(
            select(Repository).where(Repository.discord_guild_id == discord_guild_id)
        )
        return found

    async def get_by_id(self, repository_id: int) -> Repository | None:
        """The repository a row already points at.

        Reading `item.repository` instead would lazy load, which an async session cannot do
        outside its own greenlet and which fails at the point of use rather than here.
        """
        return await self._session.get(Repository, repository_id)

    async def with_boards(self) -> Sequence[Repository]:
        """Every repository that has a board linked to it, oldest first.

        Unbounded, because the question is which boards to read, and cutting the list short would
        silently stop mirroring somebody's board with nothing saying so. What bounds it is that a
        board has to be linked by hand.
        """
        return (
            await self._session.scalars(
                select(Repository)
                .where(Repository.project_number.is_not(None))
                .order_by(Repository.id)
            )
        ).all()

    async def mirroring(self, *, project_number: int, owner: str) -> Sequence[Repository]:
        """Every repository mirroring one board, however its owner was written down.

        Two repositories sharing a board would each mirror every draft card into their own
        server, because a tracked item is keyed by repository, and nothing else would notice.

        A board is a NUMBER under an ACCOUNT - GitHub keeps the sequence per account, so every
        account's first board is #1 - and the row stores the account two ways: null where it is
        the repository's own owner, and named where it is somebody else's. Compared as stored,
        that was wrong in both directions. Two servers each linking their OWN account's #1 were
        one board, so the second was refused and told the first one's repository name; and one
        server naming another's board by its owner was a different board, so it was allowed -
        after which the poll that looks a board's credential up by the board read it under the
        wrong server's member. Issue #201.

        So the owner is resolved in the query, to the one the board is actually read under: the
        stored one, or the repository's own where none is stored. That is the rule
        `domain.board.board_owner` applies in Python, and it needs no GitHub call, which is what
        the comparison-as-stored was avoiding. Lowercased on both sides, because GitHub's logins
        are case-insensitive and the person typing one is not careful about it.

        Every match rather than the first. A pair linked before this check existed can still be
        sitting in the table, and handing back whichever row the database returned first would
        make that decision for the caller - which, for a credential, is the decision that matters.
        """
        own_owner = func.split_part(Repository.repo_name, "/", 1)
        effective = func.lower(func.coalesce(Repository.project_owner, own_owner))
        found = await self._session.scalars(
            select(Repository)
            .where(Repository.project_number == project_number, effective == owner.lower())
            .order_by(Repository.id)
        )
        return found.all()

    async def set_board(
        self,
        repository: Repository,
        *,
        project_number: int | None,
        project_owner: str | None,
        linked_by: int | None = None,
    ) -> None:
        """Point a repository at a board, or at none.

        `project_number=None` clears all three, because an owner without a number addresses
        nothing and would sit in the row looking like configuration - and a member recorded as
        having authorised a board this server no longer mirrors is worse than nothing, because it
        is the thing a poll would go looking for a credential under.
        """
        repository.project_number = project_number
        repository.project_owner = project_owner if project_number is not None else None
        # Whose authorisation the board's own reads are made under. Issue #170.
        repository.project_linked_by = linked_by if project_number is not None else None
        # All three written every time. The ORM leaves a column out of the UPDATE where the new
        # value equals the one this session read, and that read took no lock - so a column another
        # writer had committed since survived beside this one's other two: a board number under
        # somebody else's owner, or a board nobody's authorisation stands behind after a link that
        # said it had linked. Found reviewing #201. Written whole, the last writer wins whole, and
        # every write waits out one still in flight.
        for column in ("project_number", "project_owner", "project_linked_by"):
            flag_modified(repository, column)
        await self._session.flush()

    async def get_by_github_id(self, github_repo_id: int) -> Repository | None:
        found: Repository | None = await self._session.scalar(
            select(Repository).where(Repository.github_repo_id == github_repo_id)
        )
        return found

    async def add(
        self,
        *,
        github_repo_id: int,
        repo_name: str,
        repo_url: str,
        discord_guild_id: int,
        private: bool | None = None,
        owner_id: int | None = None,
    ) -> Repository:
        repository = Repository(
            github_repo_id=github_repo_id,
            repo_name=repo_name,
            repo_url=repo_url,
            discord_guild_id=discord_guild_id,
            private=private,
            github_owner_id=owner_id,
        )
        self._session.add(repository)
        await self._session.flush()
        return repository

    async def follow_rename(
        self,
        repository: Repository,
        *,
        repo_name: str,
        repo_url: str,
        private: bool | None = None,
        owner_id: int | None = None,
    ) -> bool:
        """Take the name, URL, visibility and owner GitHub is using now, reporting whether the
        NAME moved.

        Webhooks find a repository by its numeric id, which survives a rename, but `/pr` and
        `/issue` compare the link against the stored name, so without this both answer that the
        link is for the wrong repository. Visibility and the owner's account id ride along on the
        same object, written only where GitHub said, which fills in a row stored before either
        column existed. Neither affects the answer: a repository quietly flipped to private has not
        been renamed.

        And a repository that moved to ANOTHER account leaves its board behind. Found reviewing
        #201. A board stored with no owner means "this repository's own owner", and a board number
        is a sequence GitHub keeps per account, so after a transfer that null quietly re-pointed the
        server at the new owner's board of the same number - a stranger's cards, read under the
        linker's authorisation. So the old owner's login is written onto the board first, unless
        both ids are known and equal: a renamed account keeps its id, and its board follows it.

        Where either id is unknown the move cannot be told from a renamed account, and GitHub hands
        a released login to whoever claims it next. So the board is also taken off its linker, which
        stops the poll reading it until somebody runs /board link - a board that stops opening,
        rather than another account's board read as this server's.

        The id follows the name it describes. A payload with no id never erases one, unless it moves
        the name to another owner: the id it would leave describes the owner being left, and kept,
        the next move would read that pairing as proof of the same account.
        """
        if (
            repository.repo_name != repo_name
            or repository.repo_url != repo_url
            or (owner_id is not None and repository.github_owner_id != owner_id)
        ):
            # The row was read without a lock, and somebody may have committed since. A /board link
            # naming an owner the pin would overwrite, or linking a board the read never saw, which
            # a move would carry to the new owner. Or another delivery moving the repository to
            # another owner - and a name or an id written from the read would then sit beside that
            # one's, pairing one account's login with another's id, which is the very proof a later
            # move is judged by. So wherever this writes the name or the id, the row is read again
            # first, held until this commits, and every question below is asked of the row as it
            # is now. Rare: a rename, or the first id a row learns. First, before anything is set,
            # because a refresh puts back what is unflushed. Visibility alone goes without: nothing
            # is decided from it.
            #
            # NO KEY UPDATE, the lock the UPDATE below takes anyway. FOR UPDATE would also wait on
            # the KEY SHARE that inserting a new item takes on this row through its foreign key,
            # and two syncs of two new items, each holding one and waiting on the other's, are a
            # deadlock. This still waits out a /board link, whose UPDATE takes the same lock.
            await self._session.refresh(repository, with_for_update={"key_share": True})

        moved = repository.repo_name != repo_name or repository.repo_url != repo_url
        revealed = private is not None and repository.private != private
        if revealed:
            logger.info("%s is now %s", repository.repo_name, "private" if private else "public")
            repository.private = private

        # Before the id below is learned: whether the account changed is a question about the row
        # as it stands.
        left = _left_behind(repository, repo_name=repo_name, owner_id=owner_id)
        if left is not None:
            _leave_behind(repository, left, repo_name=repo_name)

        if owner_id is not None:
            rewritten = repository.github_owner_id != owner_id
            if rewritten:
                repository.github_owner_id = owner_id
        else:
            rewritten = (
                _owner_of(repository.repo_name).casefold() != _owner_of(repo_name).casefold()
                and repository.github_owner_id is not None
            )
            if rewritten:
                repository.github_owner_id = None

        if not moved:
            # A delivery saying nothing new must leave the row completely alone, or `updated_at`
            # moves on every unrelated event and stops meaning anything. Flushed where something
            # did change, so that write does not wait for whatever commits next.
            if revealed or rewritten:
                await self._session.flush()
            return False

        logger.info("%s is now %s, following the rename", repository.repo_name, repo_name)
        repository.repo_name = repo_name
        repository.repo_url = repo_url
        await self._session.flush()
        return True


def _owner_of(repo_name: str) -> str:
    return repo_name.partition("/")[0]


def _left_behind(
    repository: Repository, *, repo_name: str, owner_id: int | None
) -> tuple[str, bool] | None:
    """The login a moving repository's board has to stay under, and whether the move is PROVEN
    to be to another account - or None where the board can follow.

    Only where all of these hold: a board is linked; it is stored as the repository's own owner's,
    as null, so a named owner is never rewritten; the owner half of the name changed, ignoring case
    as GitHub does; and the two are not the same account. Proven one way or the other only with the
    id on both sides, since a login is exactly the thing in question.
    """
    owner = _owner_of(repository.repo_name)
    if (
        repository.project_number is None
        or repository.project_owner is not None
        or owner.casefold() == _owner_of(repo_name).casefold()
    ):
        return None
    known = owner_id is not None and repository.github_owner_id is not None
    if known and repository.github_owner_id == owner_id:
        return None
    return owner, known


def _leave_behind(repository: Repository, left: tuple[str, bool], *, repo_name: str) -> None:
    """Write the owner being left onto the board, and say so with what to run.

    A proven move keeps its linker: the board is still the one they linked, read as before. One
    that is not proven loses the linker as well, so the poll stops reading it until somebody links
    it again - and the line names who that was, because nothing else will. /board unlink finds
    whose authorisation to let go of on the row, so after this it finds nobody, and any they still
    hold stays until they run /board withdraw.
    """
    owner, proven = left
    repository.project_owner = owner
    if proven:
        logger.warning(
            "%s moved to %s, which is another account, so its board stays under %s; run /board "
            "link to mirror a different one",
            repository.repo_name,
            repo_name,
            owner,
        )
        return
    linker = repository.project_linked_by
    repository.project_linked_by = None
    linked = (
        ""
        if linker is None
        else f". Discord member {linker} linked it, and /board unlink will not find them now, "
        "so any authorisation they still hold in this server stays until they run /board withdraw"
    )
    logger.warning(
        "%s moved to %s and nothing on record says whether that is the same account, so its board "
        "is kept under %s and not read until somebody runs /board link%s",
        repository.repo_name,
        repo_name,
        owner,
        linked,
    )
