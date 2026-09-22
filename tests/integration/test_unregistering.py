"""Proving who you are on GitHub, and then unbinding a repository.

Issue #98. `/unregister` destroys a binding and cascades through every mirror record under it, so
what it refuses is more important than what it does. The thing that makes it safe to offer at all
is that the caller proved to GitHub they hold admin, which is exactly what a Discord role and a
`/link` row cannot establish.

Driven against a real database because the single-use state is one SQL statement whose whole job
is to settle a race, and against `httpx.MockTransport` because the OAuth exchange has a failure
mode that is easy to get wrong: GitHub answers a bad code with **200 and an error in the body**.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.api.routes import oauth
from shannon.db.models import Repository, TrackedItem, VerifiedIdentity
from shannon.db.stores.identities import IdentityVerificationStore
from shannon.domain.enums import ObjectType
from shannon.domain.errors import NotProvenError, NotRegisteredError, RepositoryMismatchError
from shannon.services.unregistration import RepositoryUnregistrationService
from shannon.services.verification import (
    PROOF_LIFETIME,
    GitHubIdentityVerification,
    VerificationError,
)

pytestmark = pytest.mark.integration

GUILD = 1
ALICE = 555
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class FakePermissions:
    """What GitHub says one account may do to one repository."""

    def __init__(self, permission: str = "admin") -> None:
        self.permission = permission
        self.asked: list[tuple[str, str, str]] = []

    async def permission_for(self, owner: str, name: str, login: str) -> str:
        self.asked.append((owner, name, login))
        return self.permission


def github_says(
    *, token: str | None = "gho_abc", login: str | None = "octocat", user_id: int | None = 583231
):
    """A GitHub that completes the OAuth round trip, and a log of what it was asked."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/access_token"):
            body: dict[str, object] = {} if token is None else {"access_token": token}
            if token is None:
                body = {"error": "bad_verification_code"}
            return httpx.Response(200, content=json.dumps(body))
        body = {}
        if login is not None:
            body["login"] = login
        if user_id is not None:
            body["id"] = user_id
        return httpx.Response(200, content=json.dumps(body))

    return handler, seen


@asynccontextmanager
async def verifying(
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Clock | None = None,
    client_secret: str = "shh",
    public_base_url: str = "https://shannon.example.com",
) -> AsyncIterator[GitHubIdentityVerification]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GitHubIdentityVerification(
            sessionmaker,
            client_id="Iv23liAbC",
            client_secret=client_secret,
            oauth_url="https://github.com",
            public_base_url=public_base_url,
            http=http,
            now=clock or Clock(),
        )


class TestHandingOutTheLink:
    async def test_the_link_carries_the_client_id_and_a_state(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

        assert link.startswith("https://github.com/login/oauth/authorize?")
        assert "client_id=Iv23liAbC" in link
        assert "state=" in link

    async def test_it_asks_for_no_scope(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A user token with the default empty scope can call `GET /user`, which is all this
        needs. Asking for more would be asking somebody to grant access to prove they have it."""
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

        assert "scope=" not in link

    async def test_the_redirect_matches_the_callback_this_service_serves(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """GitHub checks it against the App's registered callback, which is the second half of the
        protection the state provides."""
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

        assert "redirect_uri=https://shannon.example.com/oauth/github/callback" in link

    async def test_every_link_is_different(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            first = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            second = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

        assert first != second

    async def test_a_half_configured_deployment_knows_it_cannot_do_this(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler, client_secret="") as verification:
            assert verification.configured is False
        async with verifying(db_sessionmaker, handler, public_base_url="") as verification:
            assert verification.configured is False


class TestRedeemingIt:
    async def test_it_answers_who_signed_in(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            verified = await verification.redeem(state=_state(link), code="abc")

        assert (verified.login, verified.github_user_id) == ("octocat", 583231)
        assert (verified.guild_id, verified.discord_user_id) == (GUILD, ALICE)

    async def test_the_user_token_is_never_written_down(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        """Used once and thrown away. One lasts eight hours and carries a six-month refresh token,
        so keeping either would mean holding a credential to somebody's whole GitHub account to
        answer a question that has already been answered."""
        handler, _ = github_says(token="gho_secret_value")

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            await verification.redeem(state=_state(link), code="abc")

        rows = (await db_session.scalars(select(VerifiedIdentity))).all()
        assert rows, "nothing was recorded at all"
        held = [
            (row.github_login, row.github_user_id, row.discord_user_id, row.verified_at)
            for row in rows
        ]
        assert all("gho_secret_value" not in str(value) for value in held)

    async def test_the_proof_is_remembered(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            await verification.redeem(state=_state(link), code="abc")

            proved = await verification.proved_just_now(guild_id=GUILD, discord_user_id=ALICE)
            assert proved is not None
            assert (proved.login, proved.github_user_id) == ("octocat", 583231)

    async def test_the_proof_does_not_go_stale_when_nothing_asks_for_a_recent_one(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other read of the same row. Whether somebody is at the keyboard now and whether
        they have ever proved anything are different questions, and only the first expires."""
        clock = Clock()
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler, clock=clock) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            await verification.redeem(state=_state(link), code="abc")
            clock.at = NOW + timedelta(days=400)

            assert await verification.proved_just_now(guild_id=GUILD, discord_user_id=ALICE) is None
            ever = await verification.ever_proved(guild_id=GUILD, discord_user_id=ALICE)
            assert ever is not None
            assert ever.github_user_id == 583231

    async def test_a_proof_goes_stale(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """It permits an irreversible command, and holding an account a while ago says little
        about holding it now."""
        clock = Clock()
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler, clock=clock) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            await verification.redeem(state=_state(link), code="abc")
            clock.at = NOW + PROOF_LIFETIME + timedelta(seconds=1)

            assert await verification.proved_just_now(guild_id=GUILD, discord_user_id=ALICE) is None

    async def test_one_link_cannot_be_redeemed_twice(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            state = _state(link)
            await verification.redeem(state=state, code="abc")

            with pytest.raises(VerificationError, match="expired or has already been used"):
                await verification.redeem(state=state, code="abc")

    async def test_a_state_nobody_issued_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same message as an expired one and an already-used one. Telling them apart would
        confirm to somebody guessing states that a particular one was real."""
        handler, seen = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            with pytest.raises(VerificationError, match="expired or has already been used"):
                await verification.redeem(state="invented", code="abc")

        assert seen == [], "it spent a round trip on a state it had already refused"

    async def test_an_expired_link_is_refused(
        self, db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await IdentityVerificationStore(db_session).issue(
            state="old", guild_id=GUILD, discord_user_id=ALICE, lifetime=timedelta(minutes=-1)
        )
        await db_session.commit()
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            with pytest.raises(VerificationError):
                await verification.redeem(state="old", code="abc")

    async def test_a_bad_code_is_caught_although_github_answers_200(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The single easiest thing to get wrong here. GitHub answers a bad code with 200 and an
        error in the body, so checking the status alone reads a refusal as a success and fails
        further along with something unrelated to say."""
        handler, _ = github_says(token=None)

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

            with pytest.raises(VerificationError, match="would not complete the sign-in"):
                await verification.redeem(state=_state(link), code="wrong")

    async def test_a_body_that_is_not_json_at_all_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """An outage page rather than an answer, which is what a proxy in front of GitHub serves
        when something has gone wrong upstream."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>an outage page</html>")

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

            with pytest.raises(VerificationError, match="would not complete the sign-in"):
                await verification.redeem(state=_state(link), code="abc")

    async def test_a_body_that_is_json_but_not_an_object_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(["not", "an", "object"]))

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

            with pytest.raises(VerificationError, match="would not complete the sign-in"):
                await verification.redeem(state=_state(link), code="abc")

    @pytest.mark.parametrize("missing", [{"login": None}, {"user_id": None}])
    async def test_a_user_github_will_not_name_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], missing: dict
    ) -> None:
        handler, _ = github_says(**missing)

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)

            with pytest.raises(VerificationError, match="would not say who signed in"):
                await verification.redeem(state=_state(link), code="abc")


class TestUnbinding:
    async def test_an_admin_unbinds_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions("admin"))

        outcome = await service.unregister(
            guild_id=GUILD, full_name=registered.repo_name, login="octocat"
        )

        assert outcome.full_name == registered.repo_name
        assert await db_session.scalar(select(Repository)) is None

    @pytest.mark.parametrize("permission", ["write", "read", "none", "maintain", ""])
    async def test_anything_short_of_admin_is_refused(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        permission: str,
    ) -> None:
        """GitHub folds `maintain` into `write` and `triage` into `read` before answering, so
        admin is the whole of the tier that may do this."""
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions(permission))

        with pytest.raises(NotProvenError, match="does not have admin"):
            await service.unregister(
                guild_id=GUILD, full_name=registered.repo_name, login="octocat"
            )

    async def test_a_refusal_leaves_the_binding_alone(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions("write"))

        with pytest.raises(NotProvenError):
            await service.unregister(
                guild_id=GUILD, full_name=registered.repo_name, login="octocat"
            )

        assert await db_session.scalar(select(Repository)) is not None

    async def test_the_permission_is_asked_about_the_registered_repository(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Not about whatever was typed. The typed name is a confirmation, and acting on it would
        let somebody be asked about a repository they do hold admin on instead."""
        github = FakePermissions("admin")
        service = RepositoryUnregistrationService(db_sessionmaker, github)

        await service.unregister(guild_id=GUILD, full_name=registered.repo_name, login="octocat")

        owner, _, name = registered.repo_name.partition("/")
        assert github.asked == [(owner, name, "octocat")]

    async def test_a_server_with_nothing_registered_is_told_so(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions())

        with pytest.raises(NotRegisteredError):
            await service.unregister(guild_id=GUILD, full_name="acme/widget", login="octocat")

    async def test_a_name_that_does_not_match_is_refused_before_github_is_asked(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        github = FakePermissions("admin")
        service = RepositoryUnregistrationService(db_sessionmaker, github)

        with pytest.raises(RepositoryMismatchError):
            await service.unregister(guild_id=GUILD, full_name="acme/something", login="octocat")

        assert github.asked == []

    async def test_the_confirmation_ignores_case_and_stray_spaces(
        self, registered: Repository, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """It is a confirmation that somebody meant it, not a password."""
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions("admin"))

        outcome = await service.unregister(
            guild_id=GUILD, full_name=f"  {registered.repo_name.upper()}  ", login="octocat"
        )

        assert outcome.full_name == registered.repo_name

    async def test_it_counts_the_threads_it_orphans(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        """Counted before the delete, because the rows cascade away with the binding. It is the
        surprising part of the command and the reply says it."""
        for number in range(3):
            db_session.add(
                TrackedItem(
                    repository_id=registered.id,
                    github_object_type=ObjectType.PR,
                    github_object_id=100 + number,
                    github_object_number=number,
                    title=f"Item {number}",
                    github_url=f"https://github.com/acme/widget/pull/{number}",
                    github_state="open",
                )
            )
        await db_session.commit()
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions("admin"))

        outcome = await service.unregister(
            guild_id=GUILD, full_name=registered.repo_name, login="octocat"
        )

        assert outcome.threads_orphaned == 3

    async def test_unbinding_takes_the_tracked_items_with_it(
        self,
        registered: Repository,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
    ) -> None:
        db_session.add(
            TrackedItem(
                repository_id=registered.id,
                github_object_type=ObjectType.PR,
                github_object_id=101,
                github_object_number=1,
                title="Item",
                github_url="https://github.com/acme/widget/pull/1",
                github_state="open",
            )
        )
        await db_session.commit()
        service = RepositoryUnregistrationService(db_sessionmaker, FakePermissions("admin"))

        await service.unregister(guild_id=GUILD, full_name=registered.repo_name, login="octocat")

        db_session.expire_all()
        assert (await db_session.scalars(select(TrackedItem))).all() == []


class TestTheCallbackRoute:
    async def test_a_redeemed_link_says_who_signed_in(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            async with _browser(verification) as client:
                response = await client.get(
                    "/oauth/github/callback", params={"code": "abc", "state": _state(link)}
                )

        assert response.status_code == 200
        assert "Signed in as octocat" in response.text

    async def test_it_does_not_echo_the_state_or_the_code_back(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """This page is on the open internet. Neither value belongs in a body or a log."""
        handler, _ = github_says()

        async with verifying(db_sessionmaker, handler) as verification:
            link = await verification.link_for(guild_id=GUILD, discord_user_id=ALICE)
            state = _state(link)
            async with _browser(verification) as client:
                response = await client.get(
                    "/oauth/github/callback", params={"code": "abc123", "state": state}
                )

        assert state not in response.text
        assert "abc123" not in response.text

    @pytest.mark.parametrize(
        "params", [{}, {"code": "abc"}, {"state": "abc"}, {"code": "", "state": ""}]
    )
    async def test_an_incomplete_link_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], params: dict
    ) -> None:
        handler, _ = github_says()

        async with (
            verifying(db_sessionmaker, handler) as verification,
            _browser(verification) as client,
        ):
            response = await client.get("/oauth/github/callback", params=params)

        assert response.status_code == 400

    async def test_a_state_nobody_issued_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with (
            verifying(db_sessionmaker, handler) as verification,
            _browser(verification) as client,
        ):
            response = await client.get(
                "/oauth/github/callback", params={"code": "abc", "state": "invented"}
            )

        assert response.status_code == 400
        assert "expired or has already been used" in response.text

    async def test_a_deployment_that_cannot_verify_says_so_rather_than_failing_oddly(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        handler, _ = github_says()

        async with (
            verifying(db_sessionmaker, handler, client_secret="") as verification,
            _browser(verification) as client,
        ):
            response = await client.get(
                "/oauth/github/callback", params={"code": "abc", "state": "anything"}
            )

        assert response.status_code == 500

    async def test_a_service_with_no_verification_wired_in_at_all(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with _browser(None) as client:
            response = await client.get(
                "/oauth/github/callback", params={"code": "abc", "state": "anything"}
            )

        assert response.status_code == 500


class TestClearingOutTheLinks:
    """`consume` marks a link spent and leaves the row; an unfollowed one is never touched.

    So the table only grew. The pruner existed and was tested against the store directly, which
    is exactly why nobody noticed it had no caller: the delivery worker's hourly sweep is what
    calls it now, and this is the seam between the two.
    """

    async def test_a_link_long_past_use_is_dropped(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handler, _ = github_says()
        await IdentityVerificationStore(db_session).issue(
            state="stale", guild_id=GUILD, discord_user_id=ALICE, lifetime=timedelta(days=-3)
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, handler) as verification:
            assert await verification.prune(keep_for=timedelta(days=1)) == 1

        assert await IdentityVerificationStore(db_session).consume("stale") is None

    async def test_a_link_somebody_could_still_follow_is_left_alone(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
    ) -> None:
        handler, _ = github_says()
        await IdentityVerificationStore(db_session).issue(
            state="live", guild_id=GUILD, discord_user_id=ALICE, lifetime=timedelta(minutes=10)
        )
        await db_session.commit()

        async with verifying(db_sessionmaker, handler) as verification:
            assert await verification.prune(keep_for=timedelta(days=1)) == 0

        assert await IdentityVerificationStore(db_session).consume("live") == (GUILD, ALICE)


def _state(link: str) -> str:
    return link.partition("state=")[2]


@asynccontextmanager
async def _browser(verification: object) -> AsyncIterator[httpx.AsyncClient]:
    """The callback route on a bare app, which is all it needs.

    Built here rather than through `create_app`, because the route reads one thing off app state
    and nothing else, and a full app would drag the whole lifespan in behind it.
    """
    app = FastAPI()
    app.state.verification = verification
    app.include_router(oauth.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
