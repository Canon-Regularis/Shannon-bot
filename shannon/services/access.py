"""Whether the person running a command may make this change on GitHub.

Issue #158. Every gate in this bot until now has been a Discord role: somebody holding Project
Manager could move any item in any repository the server is bound to, whatever GitHub thought of
them. That is the right answer for a server that has not connected anybody's account and the
wrong one for a server that has, where GitHub already knows who may write here and was never
asked.

This asks it, and only where the question can be answered. A caller who has never proved an
account is let through on the Discord role alone, which is what happened before this existed.
The fallback is the whole posture rather than a corner of it: a second lock added on top of a
working system must not become the reason the system stops.

What that costs is written down rather than glossed. Never running `/link` is a way to keep the
old behaviour, so this raises the floor for everybody who has connected an account and does not
close the door on anybody who has not. Closing it needs a setting that refuses an unproved
caller outright, and that is a decision about a deployment rather than a default.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import ProvedAccount
from shannon.db.stores.repositories import RepositoryStore
from shannon.github import people
from shannon.github.errors import GitHubError

logger = logging.getLogger(__name__)


class ReadsPermissions(Protocol):
    """What one GitHub account may do to one repository.

    The only question asked of GitHub here, so the service that can refuse a command holds no
    handle that can also write a label. Declared again rather than imported from the
    unregistration service, which asks the identical question: a consumer's Protocol says what
    THIS caller needs, and sharing one would tie two services together through a third.
    """

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...


class ProvesAccounts(Protocol):
    """The GitHub account somebody has proved they hold, if they have proved one.

    One member rather than two. The verification service also answers whether the OAuth round
    trip is configured at all, and this does not need asking: a deployment that cannot run it has
    no proofs, so every caller answers None and this gate is inert by construction rather than by
    a check.
    """

    async def ever_proved(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None: ...


class GitHubAccess:
    """Answers whether a caller's GitHub account may make the change they asked for."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: ReadsPermissions,
        proof: ProvesAccounts,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._proof = proof

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, at_least: str
    ) -> str | None:
        """The sentence to say instead of running the command, or None to let it run.

        The sentence is built here rather than in the guard, so that nothing under
        `shannon/commands/` has to know what GitHub's permission words are, and so the wording
        sits where the style tests can read it.

        Four ways to answer None, and only one of them is the caller having enough access.
        """
        proved = await self._proof.ever_proved(guild_id=guild_id, discord_user_id=discord_user_id)
        if proved is None:
            # The fallback, and the whole security posture in one branch. Nobody proved who this
            # is, so there is no GitHub account to ask about and the Discord role stands alone -
            # which is what decided every command in this bot before this existed.
            return None

        async with self._sessionmaker() as session:
            stored = await RepositoryStore(session).get_by_guild(guild_id)
        if stored is None:
            # Nothing registered, so there is no repository to ask GitHub about. The command is
            # about to fail on its own lookup with a sentence that says so; a second one here
            # would only get there first and say it worse.
            return None

        owner, _, name = stored.repo_name.partition("/")
        try:
            permission = await self._github.permission_for(owner, name, proved.login)
        except GitHubError as unreachable:
            # Fail open. An outage at GitHub must not turn every gated command in every server
            # into a refusal - the same reasoning `ItemPeople` already applies to the proof
            # check beside it. It is a real hole and it is the honest trade: anybody who can
            # wait for an outage gets the Discord-role-only behaviour back for its duration.
            logger.warning(
                "could not ask GitHub what %s may do to %s, so the Discord role decides: %s",
                proved.login,
                stored.repo_name,
                unreachable.message,
            )
            return None

        if people.at_least(permission, at_least):
            return None
        return _refusal(proved, stored.repo_name, permission)


def _refusal(proved: ProvedAccount, full_name: str, permission: str) -> str:
    """Why GitHub would not have let this person make this change.

    Both sentences name the account. Somebody may be signed in as one they forgot they proved,
    or one they have since renamed on GitHub - which reads to this bot as an account that is not
    a collaborator, because a login is asked about by name. Naming it is what makes the answer
    something they can act on rather than something they have to guess at.
    """
    if permission == people.NO_ACCESS:
        return (
            f"GitHub does not have {proved.login} as a collaborator on {full_name}, so this bot "
            "will not change anything there for you. Ask for access on GitHub, or run /link "
            "again if you have changed account since you last did."
        )
    return (
        f"You are signed in as {proved.login}, who can read {full_name} but cannot write to it, "
        "so this bot will not make that change for you. Triage counts as read here, which is "
        "GitHub's own folding rather than a rule of this bot's."
    )
