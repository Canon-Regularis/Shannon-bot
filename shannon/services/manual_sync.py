from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError, ShannonError
from shannon.github.client import GitHubClient
from shannon.github.urls import parse_pull_request_url
from shannon.services.pr_sync import PullRequestSyncService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ManualSyncOutcome:
    thread_id: int
    created: bool
    number: int
    full_name: str


class SyncFailedError(ShannonError):
    """The item was fetched but Discord could not be brought in line with it."""


class ManualPullRequestSync:
    """Backs `/pr <link>`.

    Fetching from the REST API rather than waiting for a webhook is the only difference from
    the webhook path; the actual syncing is the same service, so a manual run and an automatic
    run cannot produce different results.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        github: GitHubClient,
        sync: PullRequestSyncService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._sync = sync

    async def sync_link(self, *, guild_id: int, link: str) -> ManualSyncOutcome:
        """Fetch a pull request by link and mirror it into this guild's Discord channel.

        Raises UnparseableLinkError for a bad or non-PR link, NotRegisteredError when the guild
        has no repository, RepositoryMismatchError when the link points somewhere else, and
        GitHubError for anything GitHub refuses.
        """
        ref = parse_pull_request_url(link)

        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_guild(guild_id)

        if repository is None:
            raise NotRegisteredError("This server has no repository yet. Run /register first.")
        if repository.repo_name.lower() != ref.full_name.lower():
            raise RepositoryMismatchError(
                f"This server is registered to {repository.repo_name}, not {ref.full_name}."
            )

        assert ref.number is not None  # parse_pull_request_url guarantees it
        snapshot = await self._github.get_pull_request(ref.owner, ref.name, ref.number)

        result = await self._sync.sync(snapshot)
        if result is None:
            raise SyncFailedError(
                "The repository is registered but has no pull request channel mapped."
            )

        logger.info("manual sync of %s#%s by guild %s", ref.full_name, ref.number, guild_id)
        return ManualSyncOutcome(
            thread_id=result.thread_id,
            created=result.created,
            number=snapshot.number,
            full_name=repository.repo_name,
        )
