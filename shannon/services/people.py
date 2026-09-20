"""Putting somebody on an item from Discord, and taking them off again.

Issue #106. The second thing in this project that writes to GitHub, after the labels behind the
`/set_*` commands, and the first that writes about a person.

**It writes to GitHub and does nothing else.** No thread post, no re-render, no row of its own.
GitHub sends the change straight back as a `review_requested` or an `assigned` delivery, and the
ordinary mirror already handles it: the block's people line is rewritten and the ping line is
posted, with `claim_notifications` making sure that happens exactly once. Doing any part of that
here as well would put two of everything in the thread, and racing the delivery to do it first
would buy nothing.

Which of the two it does is told to it, not worked out from the item. That used to be inferred,
which made a pull request mean "reviewer" and an issue mean "assignee", and it left no way to
assign a pull request at all: GitHub keeps assignees and reviewers as separate lists on one item and
somebody can be on both. Issue #105 is that gap.

The one thing still decided from the item is the refusal: an issue has no reviewers, so asking for
a review in one is turned away before anything is read.
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
    """Changing who GitHub has on an item, and asking whether it would take somebody.

    Its own protocol rather than the whole client, following the rule `client.py` states three
    times: a handle that can put a reviewer on a pull request has no business also being able to
    read a commit or write a label.
    """

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

    The role rather than the object type, because the reply cares which of the two lists somebody
    went on and not which endpoint it took to get there. Both lists exist on a pull request.
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
        """The whole of all four commands, which differ by two flags and four sentences."""
        found = await locate(self._sessionmaker, thread_id)
        read = self._reads.get(found.object_type)
        if read is None:
            # A project board card. It has no page on GitHub and nobody to put on it.
            raise WorkflowRefusedError(
                f"{found.full_name} is a project board card, so there is nobody to put on it."
            )

        login = await self._login_of(found, discord_user_id)
        snapshot = await read(found.owner, found.name, found.number)
        _refuse_a_different_repository(found, snapshot.repository.github_repo_id)

        # The `isinstance` is still how the reviewer half is reached, because only a pull request
        # carries reviewers and both checkers have to be able to see that here. What changed is
        # that it no longer DECIDES anything: the role did that, and this only proves the item can
        # hold it.
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

        Refused here rather than guessed at. Nothing else in this project can turn a Discord member
        into a GitHub login, and writing to GitHub on a guess would put a stranger on somebody's
        pull request.
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

        The assignability question is asked on the assignee path only, and it is not tidiness.
        GitHub refuses a reviewer it will not take, loudly, with a 422 this project reads. It does
        not refuse an assignee: it drops them and answers as though it had done what was asked, so
        without asking first the command would report success for nothing having happened.

        It is a repository-level question rather than an item-level one, which is why the same call
        serves a pull request and an issue.
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
    """How the log line names what happened, across the four of them."""
    if role is ActorRole.REVIEWER:
        return "was asked to review" if adding else "was taken off the reviewers"
    return "was assigned" if adding else "was unassigned"


def _refuse_a_different_repository(found: FoundItem, fetched: int) -> None:
    """Refuse an answer that came from somebody else's repository.

    The fetch addresses GitHub by the stored `owner/name`, and a name is not an identity: GitHub
    frees one the moment a repository is renamed, transferred or deleted. Unchecked, a write built
    on that answer puts a reviewer on a stranger's pull request.

    The third copy of this guard in the project, beside the one in `ItemWorkflow._fetch` and the
    one in `sync/regenerate.py`. It wants promoting to one place, and that is left until the
    branches it would touch are no longer in flight.
    """
    if fetched != found.github_repo_id:
        raise RepositoryMismatchError(
            f"{found.full_name} is not the repository this server registered any more. "
            "It has been renamed or replaced on GitHub, and somebody else holds that name now."
        )
