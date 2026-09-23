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

from shannon.db.stores.identities import ProvedAccount
from shannon.db.stores.user_links import LinkedAccount, UserLinkStore
from shannon.domain.enums import ActorRole, ObjectType
from shannon.domain.errors import RepositoryMismatchError
from shannon.domain.models import Fetcher, PullRequestSnapshot
from shannon.github import people
from shannon.github.errors import GitHubError
from shannon.services.workflow import (
    FoundItem,
    WorkflowRefusedError,
    locate,
)

logger = logging.getLogger(__name__)


class PutsPeopleOnItems(Protocol):
    """Changing who GitHub has on an item, asking whether it would take somebody, and why not.

    The last two are separate questions and separate endpoints. One answers whether the write
    would land, and only ever yes or no; the other answers what the account may do here, which is
    what turns a refusal into something the person reading it can act on.
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

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...

    async def user_login(self, account_id: int) -> str | None: ...


class ProvesAccounts(Protocol):
    """Whether GitHub has ever vouched that a Discord member holds a GitHub account.

    Ever, rather than lately. `/unregister` wants a proof from minutes ago because it permits
    something irreversible; this asks whether a stored link was ever more than somebody's say-so,
    and that does not go stale.

    `configured` is here because a deployment with no public URL cannot run the round trip at
    all, and refusing a command nobody in that server could satisfy is only a way to break it.
    """

    @property
    def configured(self) -> bool: ...

    async def ever_proved(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None: ...


@dataclass(frozen=True, slots=True)
class ActingAs:
    """The GitHub account a command is about to act as, and whether anybody vouched for it."""

    login: str
    proved: bool


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
    # Whether GitHub has ever vouched that this Discord member holds this account. Carried out to
    # the reply rather than logged, because the person who needs to know is the one reading it:
    # an unproved link is one somebody typed, and this is the only place anybody finds out.
    proved: bool = True


class ItemPeople:
    """Puts one person on the item whose thread this is, in one of its two roles."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: PutsPeopleOnItems,
        reads: Mapping[ObjectType, Fetcher],
        proof: ProvesAccounts,
        *,
        require_proved: bool = False,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._reads = reads
        self._proof = proof
        self._require_proved = require_proved

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

        acting = await self._acting_as(found, discord_user_id)
        login = acting.login
        snapshot = await read(found.owner, found.name, found.number)
        _refuse_a_different_repository(found, snapshot.repository.github_repo_id)

        # The role has already chosen this branch; the `isinstance` only proves the item can
        # hold a reviewer.
        if role is ActorRole.REVIEWER:
            if not isinstance(snapshot, PullRequestSnapshot):
                raise WorkflowRefusedError(
                    f"{found.full_name}#{found.number} is an issue, and an issue has no reviewers. "
                    "Run /assign to put somebody on it instead."
                )
            change = people.reviewer_change(login, snapshot, adding=adding)
        else:
            change = people.assignee_change(login, snapshot, adding=adding)
        if change.refusal is not None:
            raise WorkflowRefusedError(change.refusal)

        await self._write(found, acting, role=role, adding=adding)

        logger.info("%s %s on %s#%s", login, _said(role, adding), found.full_name, found.number)
        return PeopleOutcome(
            login=login,
            # Off the snapshot rather than the row, which the rename guard above has just proved
            # is the same repository but not necessarily under the same name.
            full_name=snapshot.repository.full_name,
            number=found.number,
            role=role,
            added=adding,
            proved=acting.proved,
        )

    async def _acting_as(self, found: FoundItem, discord_user_id: int) -> ActingAs:
        """Which GitHub account this Discord member claimed, as GitHub names it today.

        Nothing else in this project turns a Discord member into a GitHub login, and writing on a
        guess would put a stranger on somebody's pull request.

        The claim is followed through the account id stored beside it, because a login is a label
        GitHub reassigns and the claim was recorded whenever somebody last ran `/link`. A name
        that has moved since reads to GitHub as nobody at all, which is how a collaborator with
        write access came to be told he had no access to the repository. Issue #133.

        Done here rather than where that refusal is written, because by then a stale login is
        already fatal for three of the four commands: the checks in `github/people.py` compare it
        against who is on the item, so taking somebody off under their new name is refused as
        somebody who was never on it, without GitHub being reached at all.
        """
        async with self._sessionmaker() as session:
            claimed = await UserLinkStore(session).account_for(
                guild_id=found.guild_id, discord_user_id=discord_user_id
            )
        if claimed is None:
            raise WorkflowRefusedError(
                f"Nobody has linked a GitHub account for <@{discord_user_id}> in this server, so "
                "there is nothing to put on the item. Run /link for them first."
            )
        login = await self._as_github_names_it(found, claimed, discord_user_id)
        return ActingAs(
            login=login, proved=await self._vouched_for(found, claimed, discord_user_id)
        )

    async def _vouched_for(
        self, found: FoundItem, claimed: LinkedAccount, discord_user_id: int
    ) -> bool:
        """Whether GitHub has vouched that this member holds the account their link points at.

        Held on the account id rather than the login, which matters both ways round. A proof
        survives a rename, because the name moved and the account did not. And it does not
        survive the link being pointed somewhere else, because that is a different account and
        nobody has vouched for it.

        A link with no id cannot be proved at all. That is the honest answer rather than a
        lenient one: those rows predate the column, so there is nothing to compare, and reading
        no evidence as a yes is what this whole path is here to stop.
        """
        if claimed.github_user_id is None:
            return False
        proved = await self._proof.ever_proved(
            guild_id=found.guild_id, discord_user_id=discord_user_id
        )
        return proved is not None and proved.github_user_id == claimed.github_user_id

    async def _as_github_names_it(
        self, found: FoundItem, claimed: LinkedAccount, discord_user_id: int
    ) -> str:
        """The claimed login, or the one GitHub uses for that account now.

        Best effort, deliberately. A row stored before the id column has nothing to ask about and
        costs no call at all, and a GitHub that cannot be reached leaves the claim standing, which
        is exactly what all four of these commands did before this existed. The one thing it must
        not do is turn a command that used to work into a failure: the call is anonymous, so it
        sits on the smaller of GitHub's two hourly budgets, and running out of that is not a
        reason to refuse to assign anybody.
        """
        if claimed.github_user_id is None:
            return claimed.login

        try:
            current = await self._github.user_login(claimed.github_user_id)
        except GitHubError as unreachable:
            logger.warning(
                "could not ask GitHub what account %s answers to now, so %s stands: %s",
                claimed.github_user_id,
                claimed.login,
                unreachable.message,
            )
            return claimed.login

        # An account GitHub no longer has is no reason to change anything: the name is all there
        # is left, and it is what every command used before this.
        if current is None or current.lower() == claimed.login:
            return claimed.login

        async with self._sessionmaker() as session, session.begin():
            await UserLinkStore(session).follow_rename(
                guild_id=found.guild_id,
                discord_user_id=discord_user_id,
                github_user_id=claimed.github_user_id,
                login=current,
            )
        return current.lower()

    def _must_be_proved(self) -> bool:
        """Whether an unproved link refuses rather than warns.

        Both halves, because the setting on its own is not enough. A deployment with no public
        URL cannot run the round trip, so `/link` refuses there too, and turning this on would
        leave every member of that server holding a link they have no way to prove and a command
        that will not act on it.
        """
        return self._require_proved and self._proof.configured

    async def _write(
        self, found: FoundItem, acting: ActingAs, *, role: ActorRole, adding: bool
    ) -> None:
        """Tell GitHub, by whichever of its four endpoints keeps this role.

        Assignability is asked on the assignee path only. GitHub refuses a reviewer it will not
        take with a 422, but it drops an assignee it will not take and answers as though it had
        done what was asked, so without asking first the command reports success for nothing. It
        is a repository-level question, so one call serves a pull request and an issue.
        """
        login = acting.login
        if not acting.proved and self._must_be_proved():
            # All four, rather than the two that add somebody. Taking a reviewer off under a name
            # nobody proved is the same claim as putting one on, and the way out of it is half a
            # minute in a browser rather than an admin typing the login again.
            raise WorkflowRefusedError(
                f"Nobody has proved that {login} is the GitHub account of the person being put "
                "on this item, so this bot will not act as them. Whoever holds that account can "
                "run /link to settle it."
            )

        if role is ActorRole.REVIEWER:
            if adding:
                await self._github.request_reviewers(found.owner, found.name, found.number, [login])
            else:
                await self._github.remove_reviewers(found.owner, found.name, found.number, [login])
            return

        if adding and not await self._github.can_be_assigned(found.owner, found.name, login):
            raise WorkflowRefusedError(await self._why_not(found, login))
        if adding:
            await self._github.add_assignees(found.owner, found.name, found.number, [login])
        else:
            await self._github.remove_assignees(found.owner, found.name, found.number, [login])

    async def _why_not(self, found: FoundItem, login: str) -> str:
        """Why GitHub would not take them, asked rather than guessed at.

        What stood here was one sentence for every refusal, saying the person had no access to
        the repository. The assignee endpoint answers 404 for somebody who is not there, for
        somebody who is there without write access, and for a login GitHub has never heard of,
        and only the first of those was what the sentence said. Issue #133.

        A diagnosis must not be able to make the answer worse, so a GitHub that will not answer
        this leaves the refusal standing and admits it could not be asked. That is a real
        possibility rather than a formality: this reads the collaborators endpoint, which is not
        the permission the assignee endpoint needs, so one can be refused where the other was not.
        """
        try:
            permission = await self._github.permission_for(found.owner, found.name, login)
        except GitHubError as unreachable:
            logger.warning(
                "could not ask why GitHub will not take %s on %s: %s",
                login,
                found.full_name,
                unreachable.message,
            )
            return f"GitHub will not put {login} on {found.full_name}, and could not be asked why."
        return people.assignment_refusal(login, found.full_name, permission)


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
