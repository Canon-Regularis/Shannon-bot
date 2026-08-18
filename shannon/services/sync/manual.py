from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.errors import (
    NotRegisteredError,
    RepositoryMismatchError,
    ShannonError,
    UnparseableLinkError,
)
from shannon.domain.models import RepositoryRef, TrackedSnapshot
from shannon.github.client import GitHubClient, LooksUpRepository
from shannon.github.errors import GitHubNotFoundError
from shannon.github.urls import parse_issue_url, parse_pull_request_url
from shannon.services.sync.items import SyncOutcome, SyncsItems

logger = logging.getLogger(__name__)

LinkParser = Callable[[str], RepositoryRef]
# Owner, name, number. The client it reads from is closed over by the wiring, so this
# service never holds one.
Fetcher = Callable[[str, str, int], Awaitable[TrackedSnapshot]]


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
        github: LooksUpRepository,
        sync: SyncsItems,
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

    async def _confirm_same_repository(
        self, repository: Repository, ref: RepositoryRef, *, guild_id: int
    ) -> None:
        """Settle a link that names a different repository against the id rather than the name.

        GitHub lets a repository be renamed and keeps its numeric id. The stored name only
        catches up when a webhook arrives, so refusing on the name alone leaves both commands
        rejecting the correct link for a repository that has only moved. This costs one API
        call, and only on the path that was about to refuse anyway.
        """
        try:
            named = await self._github.get_repository(ref.owner, ref.name)
        except GitHubNotFoundError:
            named = None

        if named is None or named.github_repo_id != repository.github_repo_id:
            raise RepositoryMismatchError(
                f"This server is registered to {repository.repo_name}, not {ref.full_name}."
            )

        async with self._sessionmaker() as session, session.begin():
            repositories = RepositoryStore(session)
            stored = await repositories.get_by_guild(guild_id)
            if stored is not None:
                await repositories.follow_rename(
                    stored, repo_name=named.full_name, repo_url=named.html_url
                )
        repository.repo_name = named.full_name

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
            await self._confirm_same_repository(repository, ref, guild_id=guild_id)

        if ref.number is None:
            # The link parsers guarantee a number, so this is a contract breach rather than
            # user error. Not an assert: those vanish under `python -O`.
            raise UnparseableLinkError(f"{link!r} has no {self._noun} number")

        snapshot = await self._fetch(ref.owner, ref.name, ref.number)
        result = await self._sync.sync(snapshot)

        if result.outcome is SyncOutcome.NOT_TRACKED:
            raise SyncFailedError(
                f"The repository is registered but has no {self._noun} channel mapped. "
                "Run /set_channel first."
            )
        if result.thread_id is None:
            raise SyncFailedError(f"That {self._noun} could not be mirrored into Discord.")

        logger.info("manual sync of %s#%s by guild %s", ref.full_name, ref.number, guild_id)
        return ManualSyncOutcome(
            thread_id=result.thread_id,
            created=result.created,
            number=snapshot.number,
            full_name=repository.repo_name,
        )


def build_pull_request_sync(
    sessionmaker: async_sessionmaker, github: GitHubClient, sync: SyncsItems
) -> ManualSync:
    return ManualSync(
        sessionmaker,
        github,
        sync,
        parse_link=parse_pull_request_url,
        fetch=lambda owner, name, number: github.get_pull_request(owner, name, number),
        noun="pull request",
    )


def build_issue_sync(
    sessionmaker: async_sessionmaker, github: GitHubClient, sync: SyncsItems
) -> ManualSync:
    return ManualSync(
        sessionmaker,
        github,
        sync,
        parse_link=parse_issue_url,
        fetch=lambda owner, name, number: github.get_issue(owner, name, number),
        noun="issue",
    )
