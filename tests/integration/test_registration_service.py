from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import ChannelMapping, Repository
from shannon.db.stores.installations import InstallationStore
from shannon.db.stores.repositories import RepositoryStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import (
    DuplicateRegistrationError,
    NotInstalledError,
    UnparseableLinkError,
)
from shannon.domain.models import RepositorySnapshot
from shannon.github.errors import GitHubNotFoundError
from shannon.github.webhooks.pull_request import parse_pull_request_event
from shannon.services.registration import RepositoryRegistrationService
from shannon.services.sync.items import build_item_sync
from shannon.services.sync.policies import PullRequestPolicy
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.db import blocked_on_a_row

pytestmark = pytest.mark.integration

REPO_LINK = "https://github.com/Canon-Regularis/Shannon-bot"
SNAPSHOT = RepositorySnapshot(
    github_repo_id=1255504909,
    owner="Canon-Regularis",
    name="Shannon-bot",
    html_url=REPO_LINK,
)


@pytest.fixture
def github() -> FakeGitHubClient:
    return FakeGitHubClient(repositories={"canon-regularis/shannon-bot": SNAPSHOT})


@pytest.fixture
def service(
    db_sessionmaker: async_sessionmaker[AsyncSession], github: FakeGitHubClient
) -> RepositoryRegistrationService:
    return RepositoryRegistrationService(db_sessionmaker, github)


async def test_registration_stores_repository_and_pr_channel(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    result = await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    assert result.full_name == "Canon-Regularis/Shannon-bot"
    assert result.pr_channel_id == 10

    repository = await db_session.scalar(select(Repository))
    assert repository is not None
    assert repository.github_repo_id == SNAPSHOT.github_repo_id
    assert repository.repo_name == "Canon-Regularis/Shannon-bot"
    assert repository.discord_guild_id == 1

    mapping = await db_session.scalar(select(ChannelMapping))
    assert mapping is not None
    assert mapping.object_type == ObjectType.PR
    assert mapping.discord_channel_id == 10


async def test_a_deep_link_still_registers_the_repository(
    service: RepositoryRegistrationService,
) -> None:
    result = await service.register(guild_id=1, channel_id=10, link=f"{REPO_LINK}/pull/7/files")

    assert result.full_name == "Canon-Regularis/Shannon-bot"


async def test_second_registration_in_the_same_guild_is_rejected(
    service: RepositoryRegistrationService, github: FakeGitHubClient, db_session: AsyncSession
) -> None:
    github.repositories["other/repo"] = RepositorySnapshot(
        github_repo_id=999, owner="other", name="repo", html_url="https://github.com/other/repo"
    )
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError, match="already registered to"):
        await service.register(guild_id=1, channel_id=11, link="https://github.com/other/repo")

    assert len((await db_session.scalars(select(Repository))).all()) == 1


async def test_same_repository_in_a_second_guild_is_rejected(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError, match="already registered to another server"):
        await service.register(guild_id=2, channel_id=20, link=REPO_LINK)

    assert len((await db_session.scalars(select(Repository))).all()) == 1


async def test_unknown_repository_is_rejected(service: RepositoryRegistrationService) -> None:
    with pytest.raises(GitHubNotFoundError):
        await service.register(guild_id=1, channel_id=10, link="https://github.com/who/what")


async def test_invalid_link_never_reaches_github(
    service: RepositoryRegistrationService, github: FakeGitHubClient
) -> None:
    with pytest.raises(UnparseableLinkError):
        await service.register(guild_id=1, channel_id=10, link="https://gitlab.com/owner/repo")

    assert github.repository_calls == []


async def test_a_rejected_registration_leaves_no_rows_behind(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    await service.register(guild_id=1, channel_id=10, link=REPO_LINK)

    with pytest.raises(DuplicateRegistrationError):
        await service.register(guild_id=2, channel_id=20, link=REPO_LINK)

    mappings = (await db_session.scalars(select(ChannelMapping))).all()
    assert len(mappings) == 1


async def test_a_burst_of_registrations_leaves_one_repository_and_no_raw_database_error(
    service: RepositoryRegistrationService, db_session: AsyncSession
) -> None:
    """A double-clicked /register, or a whole team running it at once.

    Whether any of them actually overlaps is up to the scheduler, so this is about the outcome
    rather than the path: one winner, one row, and nobody holding a database error. The test
    below is the one that guarantees the losing path is taken.
    """
    results = await asyncio.gather(
        *(service.register(guild_id=1, channel_id=10 + n, link=REPO_LINK) for n in range(8)),
        return_exceptions=True,
    )

    unexpected = [
        r
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, DuplicateRegistrationError)
    ]
    assert unexpected == [], (
        f"a concurrent /register raised a database error at somebody: {unexpected}"
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1, f"{len(winners)} callers were told they had registered the repository"
    assert len((await db_session.scalars(select(Repository))).all()) == 1
    assert len((await db_session.scalars(select(ChannelMapping))).all()) == 1


async def test_a_registration_that_commits_between_the_check_and_the_insert(
    service: RepositoryRegistrationService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The losing path, arranged rather than raced.

    Somebody else's row exists but has not committed, so every check this caller makes comes
    back empty and it inserts, which is where it stops and waits. The other transaction then
    commits and the unique index refuses. That is a real sequence and the only one that reaches
    the branch, and leaving it to eight concurrent callers reaches it on some runs and not on
    others, which shows up as coverage that moves on its own.
    """
    async with db_sessionmaker() as blocker:
        await blocker.begin()
        blocker.add(
            Repository(
                github_repo_id=SNAPSHOT.github_repo_id,
                repo_name=SNAPSHOT.full_name,
                repo_url=SNAPSHOT.html_url,
                discord_guild_id=1,
            )
        )
        await blocker.flush()

        racing = asyncio.create_task(service.register(guild_id=1, channel_id=10, link=REPO_LINK))
        await blocked_on_a_row(
            db_sessionmaker,
            racing,
            because=(
                "the registration finished without ever blocking on the index, so the row it "
                "was meant to collide with was not there"
            ),
        )

        await blocker.commit()

    with pytest.raises(DuplicateRegistrationError, match="a moment ago"):
        await racing

    db_session.expire_all()
    assert len((await db_session.scalars(select(Repository))).all()) == 1


class TestARepositoryRenamedOnGitHub:
    """Webhooks find a repository by its numeric id, but /pr compares the link by name."""

    async def test_the_stored_name_follows_the_rename(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        # Read before expiring, or the attribute reload would be sync IO in an async test.
        repository_id = registered.id

        await service.sync(_renamed_to("Shannon"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None
        assert stored.repo_name == "Canon-Regularis/Shannon"
        assert stored.repo_url == "https://github.com/Canon-Regularis/Shannon"

    async def test_an_unchanged_name_is_left_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        threads: FakeThreadGateway,
        pr_event,
    ) -> None:
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        repository_id, before = registered.id, registered.updated_at

        await service.sync(pr_event("edited"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None and stored.updated_at == before

    async def test_a_stale_delivery_does_not_put_the_old_name_back(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        threads: FakeThreadGateway,
    ) -> None:
        """Every payload carries the name as it was when GitHub sent it, late ones included."""
        service = build_item_sync(db_sessionmaker, threads, PullRequestPolicy())
        repository_id = registered.id
        await service.sync(_renamed_to("Shannon", at="2026-08-12T12:00:00Z"))

        await service.sync(_at_the_old_name(at="2026-08-01T12:00:00Z"))

        db_session.expire_all()
        stored = await db_session.get(Repository, repository_id)
        assert stored is not None
        assert stored.repo_name == "Canon-Regularis/Shannon"


def _renamed_to(name: str, *, at: str | None = None):
    """The same repository, under the name GitHub reports after a rename."""
    payload = payloads.pull_request_event("edited", **({"updated_at": at} if at else {}))
    payload["repository"]["name"] = name
    payload["repository"]["full_name"] = f"Canon-Regularis/{name}"
    payload["repository"]["html_url"] = f"https://github.com/Canon-Regularis/{name}"
    snapshot = parse_pull_request_event("edited", payload)
    assert snapshot is not None
    return snapshot


def _at_the_old_name(*, at: str):
    payload = payloads.pull_request_event("edited", updated_at=at)
    snapshot = parse_pull_request_event("edited", payload)
    assert snapshot is not None
    return snapshot


class FakeInstallations:
    """Stands in for the token minter, answering only what registration asks of it."""

    def __init__(self, *, installation: int | None = 42, slug: str = "shannon-bot") -> None:
        self.installation = installation
        self.slug = slug
        self.asked: list[tuple[str, str]] = []

    async def installed_on(self, owner: str, name: str) -> int | None:
        self.asked.append((owner, name))
        return self.installation

    async def app_slug(self) -> str:
        return self.slug


class TestARepositoryTheAppIsNotInstalledOn:
    """Issue #98, and the reason the whole GitHub App exists here.

    Before this, a private repository answered 404 from `GET /repos/...` and the reply said GitHub
    could not find it. The person went and checked the spelling of a link that was correct, and
    nothing anywhere mentioned that the bot simply had no access.
    """

    async def test_it_is_refused_before_github_is_asked_about_the_repository(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The order is the fix rather than an optimisation. Read first and a private repository
        reports as missing; ask first and the answer is both true and actionable."""
        github = FakeGitHubClient()
        service = RepositoryRegistrationService(
            db_sessionmaker, github, FakeInstallations(installation=None)
        )

        with pytest.raises(NotInstalledError):
            await service.register(guild_id=1, channel_id=99, link="https://github.com/acme/secret")

        assert github.repository_calls == [], "it went looking for a repository it cannot see"

    async def test_the_reply_says_what_to_do_about_it(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        service = RepositoryRegistrationService(
            db_sessionmaker, FakeGitHubClient(), FakeInstallations(installation=None)
        )

        with pytest.raises(NotInstalledError) as refusal:
            await service.register(guild_id=1, channel_id=99, link="https://github.com/acme/secret")

        assert "acme/secret" in refusal.value.message
        assert "https://github.com/apps/shannon-bot/installations/new" in refusal.value.message

    async def test_a_deployment_whose_slug_could_not_be_read_still_says_the_useful_part(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The link makes the message nicer and is not the message. Failing the command because
        GitHub would not say what the App is called would be the wrong trade."""
        service = RepositoryRegistrationService(
            db_sessionmaker, FakeGitHubClient(), FakeInstallations(installation=None, slug="")
        )

        with pytest.raises(NotInstalledError) as refusal:
            await service.register(guild_id=1, channel_id=99, link="https://github.com/acme/secret")

        assert "Install the GitHub App" in refusal.value.message
        assert "https://github.com/apps" not in refusal.value.message

    async def test_nothing_is_written_down(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        service = RepositoryRegistrationService(
            db_sessionmaker, FakeGitHubClient(), FakeInstallations(installation=None)
        )

        with pytest.raises(NotInstalledError):
            await service.register(guild_id=1, channel_id=99, link="https://github.com/acme/secret")

        assert await RepositoryStore(db_session).get_by_guild(1) is None


class TestARepositoryTheAppCanSee:
    async def test_registering_records_the_installation_on_the_way_past(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """`/register` is the one command that always knows the answer, so it is the cheapest
        place to learn it. Every call about that owner afterwards resolves from the database."""
        service = RepositoryRegistrationService(
            db_sessionmaker,
            FakeGitHubClient(repositories={"canon-regularis/shannon-bot": SNAPSHOT}),
            FakeInstallations(installation=42),
        )

        await service.register(guild_id=1, channel_id=99, link=REPO_LINK)

        found = await InstallationStore(db_session).for_owner("Canon-Regularis")
        assert found is not None
        assert found.installation_id == 42

    async def test_a_private_repository_is_recorded_as_private(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The whole point of the issue, end to end: it registers rather than reporting as
        missing, and the row says what it is."""
        service = RepositoryRegistrationService(
            db_sessionmaker,
            FakeGitHubClient(
                repositories={"canon-regularis/shannon-bot": replace(SNAPSHOT, private=True)}
            ),
            FakeInstallations(installation=42),
        )

        await service.register(guild_id=1, channel_id=99, link=REPO_LINK)

        stored = await RepositoryStore(db_session).get_by_guild(1)
        assert stored is not None
        assert stored.private is True

    async def test_a_service_built_without_the_check_behaves_as_it_always_did(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The seam is optional so that everything written before the App existed goes on working,
        including a deployment that has not set one up yet."""
        service = RepositoryRegistrationService(
            db_sessionmaker,
            FakeGitHubClient(repositories={"canon-regularis/shannon-bot": SNAPSHOT}),
        )

        result = await service.register(guild_id=1, channel_id=99, link=REPO_LINK)

        assert result.full_name == "Canon-Regularis/Shannon-bot"
