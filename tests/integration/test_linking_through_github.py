"""Following a one-time link is the whole of linking a GitHub account. Issue #144.

`/link` used to record a login somebody typed, which GitHub was never asked about, and `/verify`
existed to ask. Two commands, one of which could be wrong about who somebody is. Now there is one,
and the click finishes it: GitHub says which account signed in and the row is written there and
then, from the same answer the proof is taken from.

Driven against a real database and a real callback, because what makes this safe is not the
command. It is that the row the browser lands on says who the link was for and what it finishes,
and that the write happens where GitHub's answer is rather than where somebody's typing was.

The other half of the same callback is `test_unregistering.py`, which must keep NOT linking
anybody — that is the case this file exists to pin from the outside.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import UserLink
from shannon.db.stores.user_links import LinkedAccount, UserLinkStore
from shannon.domain.enums import VerificationPurpose
from shannon.services.linking import UserLinkingService
from shannon.services.verification import GitHubIdentityVerification

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
BOB = 777
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def github_says(*, login: str = "octocat", user_id: int = 583231):
    """A GitHub that completes the round trip and names one account."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, content=json.dumps({"access_token": "gho_abc"}))
        return httpx.Response(200, content=json.dumps({"login": login, "id": user_id}))

    return handler


@asynccontextmanager
async def verifying(
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[GitHubIdentityVerification]:
    """The real service over a real linking service, which is the point of this file.

    A stand-in for the linking half would prove the call was made and nothing about what it
    wrote, and what it writes is the whole question here.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GitHubIdentityVerification(
            sessionmaker,
            UserLinkingService(sessionmaker),
            client_id="Iv23liAbC",
            client_secret="shh",
            oauth_url="https://github.com",
            public_base_url="https://shannon.example.com",
            http=http,
            now=lambda: NOW,
        )


def _state(link: str) -> str:
    return link.partition("state=")[2]


async def followed(
    verification: GitHubIdentityVerification,
    *,
    purpose: VerificationPurpose,
    discord_user_id: int = ALICE,
) -> None:
    """Hand out a link for somebody and have them open it."""
    link = await verification.link_for(
        guild_id=GUILD, discord_user_id=discord_user_id, purpose=purpose
    )
    await verification.redeem(state=_state(link), code="abc")


class TestFollowingALinkLinksYou:
    async def test_the_row_is_written_by_the_click(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The whole of issue #144: no second command, and nothing typed."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_it_records_the_account_github_named_and_never_one_from_the_url(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The id as well as the name, off the same `GET /user` the proof is taken from. A login
        is a label GitHub reassigns; the id is the half that lasts."""
        async with verifying(
            db_sessionmaker, github_says(login="TheOctocat", user_id=999)
        ) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="theoctocat", github_user_id=999)

    async def test_the_proof_is_recorded_either_way(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The proof lands before anything is decided with it, and unconditionally. That ordering
        is what keeps the other half of this callback untouched."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.UNREGISTER)
            proved = await verification.ever_proved(guild_id=GUILD, discord_user_id=ALICE)

        assert proved is not None
        assert (proved.login, proved.github_user_id) == ("octocat", 583231)

    async def test_an_unregister_callback_writes_no_link(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The case the purpose column exists for. `/unregister` wants the proof and nothing
        else: it has an irreversible thing to do next and it needs somebody to report the answer
        to, so the browser page sends them back rather than finishing anything."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.UNREGISTER)

        assert (
            await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
            is None
        )

    async def test_it_is_written_for_whoever_the_link_was_issued_for(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The row carries the Discord account, and the URL is a bearer credential for exactly
        that: whoever opens it is recorded as that person. Which is why nothing ever hands one
        out for somebody other than the person in front of it."""
        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK, discord_user_id=BOB)

        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=GUILD, discord_user_id=BOB) is not None
        assert await store.account_for(guild_id=GUILD, discord_user_id=ALICE) is None


class TestAProofBeatsAClaim:
    async def test_it_takes_a_login_somebody_else_had_claimed(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Reached end to end for the first time. Somebody had typed this login against their own
        Discord account back when typing one was how linking worked; GitHub has now said whose it
        is, and the row that goes is the one nobody ever vouched for."""
        await UserLinkStore(db_session).link(
            guild_id=GUILD, github_username="octocat", github_user_id=583231, discord_user_id=BOB
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        db_session.expunge_all()
        store = UserLinkStore(db_session)
        assert await store.account_for(guild_id=GUILD, discord_user_id=ALICE) == LinkedAccount(
            login="octocat", github_user_id=583231
        )
        assert await store.account_for(guild_id=GUILD, discord_user_id=BOB) is None

    async def test_it_replaces_whatever_that_member_had_before(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """The other way a wrong link is fixed, and the one that needs no admin: somebody bound to
        an account that was never theirs signs in as their own."""
        await UserLinkStore(db_session).link(
            guild_id=GUILD,
            github_username="somebody-else",
            github_user_id=111,
            discord_user_id=ALICE,
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, github_says()) as verification:
            await followed(verification, purpose=VerificationPurpose.LINK)

        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="octocat", github_user_id=583231)

    async def test_two_outstanding_links_both_work_and_the_last_one_wins(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Running the command twice before clicking leaves two live states. Both are spendable,
        and if the browser has changed GitHub account in between the second click wins — which is
        what a proof beating a claim means applied to two proofs."""
        async with verifying(db_sessionmaker, github_says()) as first:
            one = await first.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
            )
            two = await first.link_for(
                guild_id=GUILD, discord_user_id=ALICE, purpose=VerificationPurpose.LINK
            )
            await first.redeem(state=_state(one), code="abc")

        async with verifying(db_sessionmaker, github_says(login="wanderer", user_id=900)) as then:
            await then.redeem(state=_state(two), code="abc")

        db_session.expunge_all()
        found = await UserLinkStore(db_session).account_for(guild_id=GUILD, discord_user_id=ALICE)
        assert found == LinkedAccount(login="wanderer", github_user_id=900)
        assert await db_session.scalar(select(UserLink).where(UserLink.discord_user_id == ALICE))
