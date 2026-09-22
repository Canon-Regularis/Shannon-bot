"""Putting somebody on an item from Discord, and taking them off again.

It writes to GitHub and does nothing else. GitHub sends the change straight back as a
`review_requested` or an `assigned` delivery and the ordinary mirror handles it, so rewriting the
people line or posting the ping here as well would put two of everything in the thread.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.user_links import UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.domain.models import Fetcher, PullRequestSnapshot
from shannon.github import people
from shannon.services.workflow import (
    FoundItem,
    WorkflowRefusedError,
    locate,
)

logger = logging.getLogger(__name__)


class PutsPeopleOnItems(Protocol):
    """Changing who GitHub has on an item, and asking whether it would take somebody."""

    async def request_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def remove_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def add_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def remove_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def can_be_assigned(self, owner: str, name: str, login: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class PeopleOutcome:
    """What was done, in the terms the reply has to say it in.

    The role rather than the object type: GitHub keeps assignees and reviewers as separate lists,
    both of which a pull request has, and somebody can be on both.
    """

    login: str
    full_name: str
    number: int
    role: ActorRole
    added: bool


class ItemPeople:
    """Puts one person on the item whose thread this is, in one of its two roles."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: PutsPeopleOnItems,
        reads: Mapping[ObjectType, Fetcher],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._reads = reads

    async def assign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return await self._change(thread_id, discord_user_id, role=ActorRole.ASSIGNEE, adding=True)

    async def unassign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return await self._change(thread_id, discord_user_id, role=ActorRole.ASSIGNEE, adding=False)

    async def request_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return await self._change(thread_id, discord_user_id, role=ActorRole.REVIEWER, adding=True)

    async def unrequest_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return await self._change(thread_id, discord_user_id, role=ActorRole.REVIEWER, adding=False)

    async def _change(
        self, thread_id: int, discord_user_id: int, *, role: ActorRole, adding: bool
    ) -> PeopleOutcome:
        found = await locate(self._sessionmaker, thread_id)
        read = self._reads.get(found.object_type)
        if read is None:
            raise WorkflowRefusedError(
                f"{found.full_name} is a project board card, so there is nobody to put on it."
            )

        login = await self._login_of(found, discord_user_id)
        snapshot = await read(found.owner, found.name, found.number)
        _refuse_a_different_repository(found, snapshot.repository.github_repo_id)

        # The role has already chosen this branch; the `isinstance` only proves the item can
        # hold a reviewer.
        if role is ActorRole.REVIEWER:
            if not isinstance(snapshot, PullRequestSnapshot):
                raise WorkflowRefusedError(
                    f"{found.full_name}#{found.number} is an issue, and an issue has no reviewers. "
                    "Use /assign to put somebody on it instead."
                )
            change = people.reviewer_change(login, snapshot, adding=adding)
        else:
            change = people.assignee_change(login, snapshot, adding=adding)
        if change.refusal is not None:
            raise WorkflowRefusedError(change.refusal)

        await self._write(found, login, role=role, adding=adding)

        logger.info("%s %s on %s#%s", login, _said(role, adding), found.full_name, found.number)
        return PeopleOutcome(
            login=login,
            # Off the snapshot rather than the row, which the rename guard above has just proved
            # is the same repository but not necessarily under the same name.
            full_name=snapshot.repository.full_name,
            number=found.number,
            role=role,
            added=adding,
        )

    async def _login_of(self, found: FoundItem, discord_user_id: int) -> str:
        """Which GitHub account this Discord member claimed, refusing if they never claimed one.

        Nothing else in this project turns a Discord member into a GitHub login, and writing on a
        guess would put a stranger on somebody's pull request.
        """
        async with self._sessionmaker() as session:
            login = await UserLinkStore(session).login_for(
                guild_id=found.guild_id, discord_user_id=discord_user_id
            )
        if login is None:
            raise WorkflowRefusedError(
                f"<@{discord_user_id}> has no GitHub account linked in this server, so there is "
                "nothing to put on the item. Run /link for them first."
            )
        return login

    async def _write(self, found: FoundItem, login: str, *, role: ActorRole, adding: bool) -> None:
        """Tell GitHub, by whichever of its four endpoints keeps this role.

        Assignability is asked on the assignee path only. GitHub refuses a reviewer it will not
        take with a 422, but it drops an assignee it will not take and answers as though it had
        done what was asked, so without asking first the command reports success for nothing. It
        is a repository-level question, so one call serves a pull request and an issue.
        """
        if role is ActorRole.REVIEWER:
            if adding:
                await self._github.request_reviewers(found.owner, found.name, found.number, [login])
            else:
                await self._github.remove_reviewers(found.owner, found.name, found.number, [login])
            return

        if adding and not await self._github.can_be_assigned(found.owner, found.name, login):
            raise WorkflowRefusedError(
                f"GitHub will not put {login} on {found.full_name}, which usually means they "
                "have no access to the repository."
            )
        if adding:
            await self._github.add_assignees(found.owner, found.name, found.number, [login])
        else:
            await self._github.remove_assignees(found.owner, found.name, found.number, [login])


def _said(role: ActorRole, adding: bool) -> str:
    if role is ActorRole.REVIEWER:
        return "was asked to review" if adding else "was taken off the reviewers"
    return "was assigned" if adding else "was unassigned"


def _refuse_a_different_repository(found: FoundItem, fetched: int) -> None:
    """Refuse an answer that came from somebody else's repository.

    The fetch addresses GitHub by the stored `owner/name`, and a name is not an identity: GitHub
    frees one the moment a repository is renamed, transferred or deleted, so an unchecked write
    puts a reviewer on a stranger's pull request. Copied in `ItemWorkflow._fetch` and
    `sync/regenerate.py`.
    """
    if fetched != found.github_repo_id:
        raise RepositoryMismatchError(
            f"{found.full_name} is not the repository this server registered any more. "
            "It has been renamed or replaced on GitHub, and somebody else holds that name now."
        )
