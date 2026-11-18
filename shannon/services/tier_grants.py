"""Whether GitHub vouches for a member the Discord role turned away. Issue #94.

A server that asks for it lets GitHub write stand in for the Developer tier, and GitHub admin for
Project Manager as well. The gate decides which level a command needs and whether GitHub may be
asked at all; this answers the one question left, for the account the member proved with `/link`
on the repository the server registered.

It fails CLOSED, where `access.py` (#158) beside it fails open, and the difference is the whole
point. That one only ever restricts somebody who already holds the role, so an outage taking it
away costs nothing the role did not already allow. This one raises access, so an answer it could
not get is no answer, and a `GitHubError` is logged and raised rather than read as anything.

Three things it holds that #158 does not need to:

- A proof only counts if it came back through Discord. Before migration `0033` a forwarded link
  signed in whoever clicked it, so an older row may name somebody else's account; `through_discord`
  is how a row says it is not one of those.
- GitHub is asked by login and answers about an account, and the two are compared by id. A login
  is a label GitHub hands to somebody else once its holder renames (see `ProvedAccount`), and a
  grant on a name alone would hand the new holder's access to the old one.
- Answers that grant nothing are remembered for a minute, so a member with no role cannot spend the
  installation's GitHub budget, which the webhook sync and the board poller share, by asking over
  and over. Answers that grant are never remembered, so access taken away on GitHub is taken away
  here at once.

The repository's id is not asked again. Every command acts on the stored name, so this asks about
exactly what the command will touch, and only its owner can make that name again in their own
namespace.

It reads the proof from its store rather than through the verification service's `ever_proved`,
which makes the same read: the gate is handed this, and verification is handed the gate through
`MemberTiers`, so going through it would make the three a construction cycle.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.stores.identities import ProvedAccount, VerifiedIdentityStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.github import people
from shannon.github.errors import GitHubError
from shannon.github.people import Standing

logger = logging.getLogger(__name__)


class ReadsStanding(Protocol):
    """What one GitHub account may do to one repository, and which account GitHub answered about.

    The only question asked of GitHub here, declared where it is consumed in the style of
    `access.ReadsPermissions`: the service that can let a member in holds no handle that could
    write anything.
    """

    async def collaborator_standing(self, owner: str, name: str, login: str) -> Standing: ...


@dataclass(frozen=True, slots=True)
class _Asked:
    """Whose answer a remembered refusal is.

    The proof's `verified_at` is in it, so running `/link` again makes a key nothing was
    remembered under and GitHub is asked afresh. The repository is in it as well, so a server
    registered to another repository within the minute is not answered about the old one.
    """

    guild_id: int
    discord_user_id: int
    github_user_id: int
    verified_at: datetime
    repo_name: str


class GitHubTierGrants:
    """Answers whether GitHub's permission for a member's proved account reaches a level."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        github: ReadsStanding,
        *,
        forget_after: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._forget_after = forget_after
        # Monotonic, as the cooldowns' clock is, so a wall clock stepped back cannot hold a
        # refusal for longer than the minute it was promised for.
        self._clock = clock
        self._remembered: dict[_Asked, tuple[float, Standing]] = {}

    async def refusal_for(self, *, guild_id: int, discord_user_id: int, level: str) -> str | None:
        """None where GitHub vouches for this member at `level`, or the sentence saying why not.

        The sentence follows the role's own denial, so it says what GitHub would have needed
        and never repeats which roles would do. Raises `GitHubError` where GitHub could not be
        asked, which every caller refuses on.
        """
        async with self._sessionmaker() as session:
            proof = await VerifiedIdentityStore(session).proved(
                guild_id=guild_id, discord_user_id=discord_user_id
            )
            repository = await RepositoryStore(session).get_by_guild(guild_id)
            # Read while the session is open, which is the only time the row can be read.
            repo_name = None if repository is None else repository.repo_name

        if repo_name is None:
            return (
                f"GitHub's {level} permission on this server's repository would let you in as "
                "well, once one is registered."
            )
        if proof is None:
            return (
                f"GitHub's {level} permission on {repo_name} would let you in as well, once /link "
                "has proved which GitHub account is yours."
            )
        if not proof.through_discord:
            return (
                f"GitHub's {level} permission on {repo_name} would let you in as well, but your "
                f"proof of {proof.login} was made before links went through Discord, so run /link "
                "once more for it to count."
            )

        asked = _Asked(
            guild_id, discord_user_id, proof.github_user_id, proof.verified_at, repo_name
        )
        standing = self._recall(asked)
        if standing is None:
            standing = await self._ask(repo_name, proof)
            if _grants_nothing(standing, proof):
                self._remember(asked, standing)

        refusal = _refusal(standing, proof, repo_name, level)
        if refusal is None:
            # The audit line. A member let in by GitHub rather than by a role is the case
            # somebody will one day want to find again, so it names everything that decided it.
            logger.info(
                "GitHub's %s permission for %s on %s let Discord member %s in at the %s level",
                standing.permission,
                proof.login,
                repo_name,
                discord_user_id,
                level,
            )
        return refusal

    async def _ask(self, repo_name: str, proof: ProvedAccount) -> Standing:
        owner, _, name = repo_name.partition("/")
        try:
            return await self._github.collaborator_standing(owner, name, proof.login)
        except GitHubError as unreachable:
            # Fail closed, the opposite of #158 and on purpose: this raises access, so GitHub
            # not answering cannot be read as GitHub saying yes. Never remembered either, so the
            # next ask after an outage is a real one.
            logger.warning(
                "could not ask GitHub what %s may do to %s, so only the Discord role counts: %s",
                proof.login,
                repo_name,
                unreachable.message,
            )
            raise

    def _recall(self, asked: _Asked) -> Standing | None:
        remembered = self._remembered.get(asked)
        if remembered is None:
            return None
        at, standing = remembered
        if self._clock() - at >= self._forget_after:
            return None
        return standing

    def _remember(self, asked: _Asked, standing: Standing) -> None:
        now = self._clock()
        # Spent answers go on the way in, as the cooldowns' do, so a member asked about once is
        # not held for the life of the process.
        self._remembered = {
            key: (at, kept)
            for key, (at, kept) in self._remembered.items()
            if now - at < self._forget_after
        }
        self._remembered[asked] = (now, standing)


def _grants_nothing(standing: Standing, proof: ProvedAccount) -> bool:
    """Whether this answer could not let the member in at any level, which is what may be kept.

    A write answer to an admin question is not one of these: it grants a Developer gate, and a
    grant is never remembered.
    """
    return standing.account_id != proof.github_user_id or not people.at_least(
        standing.permission, people.WRITE
    )


def _refusal(standing: Standing, proof: ProvedAccount, repo_name: str, level: str) -> str | None:
    """None where the answer reaches `level` for the proved account, or the sentence that is true.

    No access comes before the account check, because GitHub names no account for somebody who
    is not there at all, and "that name belongs to somebody else now" would be a guess. Admin in
    these sentences is always "GitHub's admin permission", so it cannot be read as the Discord
    role Admin the denial before it may have just named.
    """
    would = f"GitHub's {level} permission on {repo_name} would let you in as well"
    if standing.permission == people.NO_ACCESS:
        return (
            f"{would}, but GitHub does not have {proof.login} as a collaborator there. Run /link "
            "again if that name is no longer yours."
        )
    if standing.account_id is None:
        return (
            f"{would}, but GitHub did not say which account {proof.login} is, so its answer "
            "could not be held against the one you proved."
        )
    if standing.account_id != proof.github_user_id:
        return (
            f"{would}, but the name {proof.login} now belongs to a different account from the "
            "one you proved, so run /link again."
        )
    if people.at_least(standing.permission, level):
        return None
    if standing.permission == people.READ_ONLY:
        return (
            f"{would}, but GitHub gives {proof.login} read access there. Triage counts as read "
            "here, which is GitHub's own folding."
        )
    return f"{would}, but GitHub gives {proof.login} {standing.permission} access there."
