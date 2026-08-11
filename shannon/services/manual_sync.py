from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.errors import NotRegisteredError, RepositoryMismatchError, ShannonError
from shannon.domain.models import RepositoryRef, TrackedSnapshot
from shannon.github.client import GitHubClient
from shannon.github.urls import parse_issue_url, parse_pull_request_url
from shannon.services.item_sync import ItemSyncService

logger = logging.getLogger(__name__)

LinkParser = Callable[[str], RepositoryRef]
Fetcher = Callable[[GitHubClient, str, str, int], Awaitable[TrackedSnapshot]]


@dataclass(frozen=True, slots=True)
class ManualSyncOutcome:
    thread_id: int
    created: bool
    number: int
    full_name: str


class SyncFailedError(ShannonError):
    """The item was fetched but Discord could not be brought in line with it."""


class ManualSync:
    """Backs the commands that sync one item by link.

    Fetching from the REST API rather than waiting for a webhook is the only difference from
    the webhook path; the syncing itself is the same service, so a manual run and an automatic
    run cannot produce different results.

    The link parser, the fetch and the noun are injected, which is all that separates `/pr`
    from `/issue`.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        github: GitHubClient,
        sync: ItemSyncService,
        *,
        parse_link: LinkParser,
        fetch: Fetcher,
        noun: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._github = github
        self._sync = sync
        self._parse_link = parse_link
        self._fetch = fetch
        self._noun = noun

    async def sync_link(self, *, guild_id: int, link: str) -> ManualSyncOutcome:
        """Fetch an item by link and mirror it into this guild's Discord channel.

        Raises UnparseableLinkError for a link of the wrong shape, NotRegisteredError when the
        guild has no repository, RepositoryMismatchError when the link points somewhere else,
        and GitHubError for anything GitHub refuses.
        """
        ref = self._parse_link(link)

        async with self._sessionmaker() as session:
            repository = await RepositoryStore(session).get_by_guild(guild_id)

        if repository is None:
            raise NotRegisteredError("This server has no repository yet. Run /register first.")
        if repository.repo_name.lower() != ref.full_name.lower():
            raise RepositoryMismatchError(
                f"This server is registered to {repository.repo_name}, not {ref.full_name}."
            )

        assert ref.number is not None  # the link parsers guarantee it
        snapshot = await self._fetch(self._github, ref.owner, ref.name, ref.number)

        result = await self._sync.sync(snapshot)
        if result is None:
            raise SyncFailedError(
                f"The repository is registered but has no {self._noun} channel mapped. "
                "Run /set_channel first."
            )

        logger.info("manual sync of %s#%s by guild %s", ref.full_name, ref.number, guild_id)
        return ManualSyncOutcome(
            thread_id=result.thread_id,
            created=result.created,
            number=snapshot.number,
            full_name=repository.repo_name,
        )


def build_pull_request_sync(
    sessionmaker: async_sessionmaker, github: GitHubClient, sync: ItemSyncService
) -> ManualSync:
    return ManualSync(
        sessionmaker,
        github,
        sync,
        parse_link=parse_pull_request_url,
        fetch=lambda client, owner, name, number: client.get_pull_request(owner, name, number),
        noun="pull request",
    )


def build_issue_sync(
    sessionmaker: async_sessionmaker, github: GitHubClient, sync: ItemSyncService
) -> ManualSync:
    return ManualSync(
        sessionmaker,
        github,
        sync,
        parse_link=parse_issue_url,
        fetch=lambda client, owner, name, number: client.get_issue(owner, name, number),
        noun="issue",
    )
