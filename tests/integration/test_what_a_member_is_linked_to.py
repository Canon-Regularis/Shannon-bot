"""What one member is linked to here, since when, and what stands behind it. Issue #232.

`UserLinkingService.standing` against a real database, so each fact `/linked` words is read from
the row that holds it: the link and its day from `user_links`, the proof from
`verified_identities`, and the repository from `repositories`. Every round trip writes a proof and
only `/link` writes a link, which is why the two are read apart and can disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, UserLink
from shannon.db.stores.identities import ProvedAccount, VerifiedIdentityStore
from shannon.db.stores.user_links import LinkedAccount, LinkRecord, UserLinkStore
from shannon.services.linking import LinkStanding, UserLinkingService

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 42
SIGNED_IN = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)


@pytest.fixture
def service(db_sessionmaker: async_sessionmaker[AsyncSession]) -> UserLinkingService:
    return UserLinkingService(db_sessionmaker)


async def linked(
    session: AsyncSession, login: str, github_user_id: int | None, *, guild_id: int = GUILD
) -> datetime:
    """Link Alice as `/link` would, and answer the day the row was made."""
    await UserLinkStore(session).link(
        guild_id=guild_id,
        github_username=login,
        github_user_id=github_user_id,
        discord_user_id=ALICE,
    )
    await session.commit()
    made = await session.scalar(
        select(UserLink.created_at).where(UserLink.discord_guild_id == guild_id)
    )
    assert made is not None
    return made


async def signed_in(
    session: AsyncSession, login: str, github_user_id: int, *, guild_id: int = GUILD
) -> ProvedAccount:
    """A round trip finished as this account, whatever it was for."""
    await VerifiedIdentityStore(session).remember(
        guild_id=guild_id,
        discord_user_id=ALICE,
        github_login=login,
        github_user_id=github_user_id,
        verified_at=SIGNED_IN,
    )
    await session.commit()
    return ProvedAccount(login=login, github_user_id=github_user_id, verified_at=SIGNED_IN)


class TestWhatAMemberIsToldAboutTheirLink:
    async def test_a_link_github_vouched_for(
        self, service: UserLinkingService, db_session: AsyncSession, registered: Repository
    ) -> None:
        day = await linked(db_session, "octocat", 583231)
        proof = await signed_in(db_session, "octocat", 583231)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing == LinkStanding(
            repo_name=registered.repo_name,
            link=LinkRecord(
                account=LinkedAccount(login="octocat", github_user_id=583231), linked_at=day
            ),
            proof=proof,
        )
        assert standing.proved

    async def test_a_later_sign_in_as_another_account_leaves_it_unproved(
        self, service: UserLinkingService, db_session: AsyncSession, registered: Repository
    ) -> None:
        """The proof is the last sign-in from this server, whatever it was for. Signing in as
        somebody else for /register replaced the one that proved the link."""
        await linked(db_session, "octocat", 583231)
        other = await signed_in(db_session, "monalisa", 200)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing.link is not None
        assert standing.proof == other
        assert not standing.proved

    async def test_a_link_by_name_alone_is_never_proved(
        self, service: UserLinkingService, db_session: AsyncSession, registered: Repository
    ) -> None:
        await linked(db_session, "octocat", None)
        await signed_in(db_session, "octocat", 583231)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing.link is not None
        assert standing.link.account == LinkedAccount(login="octocat", github_user_id=None)
        assert not standing.proved

    async def test_a_sign_in_on_its_own_is_not_a_link(
        self, service: UserLinkingService, db_session: AsyncSession, registered: Repository
    ) -> None:
        """The likeliest "I thought I was linked": GitHub knows them from /register, and nothing
        here links them."""
        proof = await signed_in(db_session, "octocat", 583231)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing == LinkStanding(repo_name=registered.repo_name, link=None, proof=proof)
        assert not standing.proved

    async def test_somebody_who_never_did_either(
        self, service: UserLinkingService, registered: Repository
    ) -> None:
        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing == LinkStanding(repo_name=registered.repo_name, link=None, proof=None)
        assert not standing.proved

    async def test_another_servers_link_and_proof_say_nothing_here(
        self, service: UserLinkingService, db_session: AsyncSession, registered: Repository
    ) -> None:
        """A link belongs to the server it was made in, and so does a proof."""
        await linked(db_session, "octocat", 583231, guild_id=2)
        await signed_in(db_session, "octocat", 583231, guild_id=2)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing == LinkStanding(repo_name=registered.repo_name, link=None, proof=None)

    async def test_a_server_with_no_repository_still_has_its_links(
        self, service: UserLinkingService, db_session: AsyncSession
    ) -> None:
        """/link never asks for a repository, and /unregister keeps links, so a link stands in a
        server that mirrors nothing."""
        day = await linked(db_session, "octocat", 583231)

        standing = await service.standing(guild_id=GUILD, discord_user_id=ALICE)

        assert standing.repo_name is None
        assert standing.link == LinkRecord(
            account=LinkedAccount(login="octocat", github_user_id=583231), linked_at=day
        )
