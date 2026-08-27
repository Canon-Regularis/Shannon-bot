from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.db.stores.user_links import UserLinkStore
from shannon.github.errors import GitHubUnavailableError
from shannon.services.linking import InvalidGitHubUsernameError, UserLinkingService
from tests.fakes.github import FakeGitHubClient

pytestmark = pytest.mark.integration


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker, FakeGitHubClient())


async def count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(UserLink)) or 0


async def test_a_link_is_stored_lowercased(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    assert (
        await service.link(guild_id=1, github_username="OctoCat", discord_user_id=42) == "OctoCat"
    )

    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "octocat"
    assert row.discord_user_id == 42


async def test_an_at_prefix_is_stripped(service: UserLinkingService) -> None:
    assert (
        await service.link(guild_id=1, github_username="@octocat", discord_user_id=42) == "octocat"
    )


@pytest.mark.parametrize("username", ["not a name", "", "  ", "-leading", "a" * 40])
async def test_a_bad_username_is_refused(service: UserLinkingService, username: str) -> None:
    with pytest.raises(InvalidGitHubUsernameError):
        await service.link(guild_id=1, github_username=username, discord_user_id=42)


async def test_relinking_the_same_discord_account_replaces_the_github_name(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="monalisa", discord_user_id=42)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.github_username == "monalisa"


async def test_moving_a_github_name_to_another_account_replaces_the_row(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=1, github_username="octocat", discord_user_id=99)

    assert await count(db_session) == 1
    row = await db_session.scalar(select(UserLink))
    assert row is not None
    assert row.discord_user_id == 99


async def test_links_are_scoped_to_a_guild(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)
    await service.link(guild_id=2, github_username="octocat", discord_user_id=77)

    assert await count(db_session) == 2


async def test_resolving_many_ignores_case_and_skips_unknowns(
    service: UserLinkingService, db_session: AsyncSession
) -> None:
    await service.link(guild_id=1, github_username="octocat", discord_user_id=42)

    resolved = await UserLinkStore(db_session).resolve_many(
        guild_id=1, github_usernames=["OctoCat", "nobody"]
    )

    assert resolved == {"octocat": 42}


async def test_resolving_nothing_asks_the_database_for_nothing(
    db_session: AsyncSession,
) -> None:
    assert await UserLinkStore(db_session).resolve_many(guild_id=1, github_usernames=[]) == {}


class TestALoginNobodyHolds:
    """A link that can never match is the one outcome this command must not record.

    The fallback for somebody who has not linked is to name them in plain text, deliberately.
    So a login with a typo in it produces exactly what an unlinked person produces: plain text
    in the ping, plain text in the block, and nothing at all in the log. There is no way for the
    person, the admin or the owner to tell the two apart, and the person simply never hears from
    the bot again.

    Proven before it was fixed, driving the real command: `/link mona--lisa`, `/link monalisa-`
    and `/link definitely-not-a-real-account` were all answered "Linked GitHub user ... to
    <@555>.", and a review requested from the real `monalisa` went out as
    "Review requested from monalisa."
    """

    @pytest.fixture
    def github(self) -> FakeGitHubClient:
        return FakeGitHubClient(users={"monalisa"})

    async def test_a_login_github_has_never_heard_of_is_refused(
        self, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient
    ) -> None:
        service = UserLinkingService(db_sessionmaker, github)

        with pytest.raises(InvalidGitHubUsernameError, match="no user called"):
            await service.link(guild_id=1, github_username="monalisaa", discord_user_id=555)

    async def test_the_one_it_has_heard_of_is_linked(
        self, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient, db_session
    ) -> None:
        service = UserLinkingService(db_sessionmaker, github)

        assert await service.link(guild_id=1, github_username="MonaLisa", discord_user_id=555)
        assert github.user_calls == ["MonaLisa"]

    @pytest.mark.parametrize("typed", ["mona--lisa", "monalisa-", "-monalisa", "mona_lisa"])
    async def test_a_shape_github_cannot_issue_never_reaches_the_network(
        self, db_sessionmaker: async_sessionmaker, github: FakeGitHubClient, typed: str
    ) -> None:
        """GitHub's rule is single hyphens, never leading or trailing. The pattern was looser,
        so these were stored; being narrower now also saves a call that could only say no."""
        service = UserLinkingService(db_sessionmaker, github)

        with pytest.raises(InvalidGitHubUsernameError, match="not a GitHub username"):
            await service.link(guild_id=1, github_username=typed, discord_user_id=555)

        assert github.user_calls == [], "it asked GitHub about a name it could rule out itself"

    async def test_github_being_unreachable_refuses_rather_than_guesses(
        self, db_sessionmaker: async_sessionmaker
    ) -> None:
        """A link that cannot be checked is worth less than the person trying again in a minute,
        and the reply table already knows how to say GitHub could not be reached."""
        unreachable = FakeGitHubClient()
        unreachable.error = GitHubUnavailableError("Could not reach GitHub")
        service = UserLinkingService(db_sessionmaker, unreachable)

        with pytest.raises(GitHubUnavailableError):
            await service.link(guild_id=1, github_username="monalisa", discord_user_id=555)
