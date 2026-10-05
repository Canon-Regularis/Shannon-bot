"""Whether the caller's GitHub account may make the change they asked for.

Issue #158, requirement 4. Every gate in this bot until now was a Discord role: somebody holding
Project Manager could move any item in any repository the server is bound to, whatever GitHub
thought of them. That is right for a server nobody has connected an account in, and wrong for one
where GitHub already knows who may write here and was never asked.

Most of what is tested here is the ways it says YES, because four of the five answers are yes and
only one of them is the caller actually having access. Each of the other three is a deliberate
hole with a reason, and a test saying which is the difference between a hole and a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository
from shannon.db.stores.identities import ProvedAccount
from shannon.github import people
from shannon.github.errors import GitHubUnavailableError
from shannon.services.access import GitHubAccess
from tests.support.round_trip import without_discord

pytestmark = pytest.mark.integration

LOGIN = "monalisa"
WHO = 424242


class FakeProof:
    def __init__(self, login: str | None = LOGIN, *, configured: bool = True) -> None:
        self.login = login
        # Whether the App's half of the round trip is set up - its client id and secret and a
        # public URL.
        # Separate from whether anybody has used it, because `require_proved` has to tell a
        # deployment that cannot produce a proof apart from a person who has not produced one.
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def ever_proved(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        if self.login is None:
            return None
        return ProvedAccount(login=self.login, github_user_id=7, verified_at=datetime.now(UTC))


class FakePermissions:
    def __init__(self, permission: str = people.WRITE, error: Exception | None = None) -> None:
        self.permission = permission
        self.error = error
        self.asked: list[tuple[str, str, str]] = []

    async def permission_for(self, owner: str, name: str, login: str) -> str:
        self.asked.append((owner, name, login))
        if self.error is not None:
            raise self.error
        return self.permission


def access(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    proof: FakeProof | None = None,
    permissions: FakePermissions | None = None,
    require_proved: bool = False,
) -> GitHubAccess:
    return GitHubAccess(
        sessionmaker,
        permissions or FakePermissions(),
        proof or FakeProof(),
        require_proved,
    )


async def refusal(service: GitHubAccess, *, at_least: str = people.WRITE) -> str | None:
    return await service.refusal_for(guild_id=1, discord_user_id=WHO, at_least=at_least)


class TestTheWaysItSaysYes:
    async def test_write_access_passes(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        assert await refusal(access(db_sessionmaker)) is None

    async def test_admin_passes_a_write_check(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """The ladder, rather than an equality. Asking for write and refusing an administrator
        is the bug a `==` would have shipped."""
        service = access(db_sessionmaker, permissions=FakePermissions(people.ADMIN))

        assert await refusal(service) is None

    async def test_a_caller_who_never_proved_an_account_passes(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
    ) -> None:
        """The fallback, and the whole posture in one branch. Nobody proved who this is, so there
        is no GitHub account to ask about and the Discord role stands alone - which is what
        decided every command in this bot before this existed.

        It is also the hole: never running /link is a way to keep the old behaviour. Closing it
        needs a setting that refuses an unproved caller outright, which is a decision about a
        deployment rather than a default.
        """
        service = access(db_sessionmaker, proof=FakeProof(login=None))

        assert await refusal(service) is None

    async def test_a_caller_who_never_proved_costs_no_github_call(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        permissions = FakePermissions()
        service = access(db_sessionmaker, proof=FakeProof(login=None), permissions=permissions)

        await refusal(service)

        assert permissions.asked == []

    async def test_a_server_with_nothing_registered_passes(
        self, db_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """There is no repository to ask GitHub about, and the command is about to fail on its
        own lookup with a sentence that says so. A second refusal here would only get there
        first and say it worse."""
        assert await refusal(access(db_sessionmaker)) is None

    async def test_github_being_unreachable_passes(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fail open, deliberately and not comfortably. An outage at GitHub must not turn every
        gated command in every server into a refusal - a second lock added on top of a working
        system must not become the reason the system stops.

        The cost is real and is not hidden: anybody who can wait for an outage gets the
        Discord-role-only behaviour back for its duration. It is logged so that a long one is
        visible rather than silent.
        """
        service = access(
            db_sessionmaker,
            permissions=FakePermissions(error=GitHubUnavailableError("GitHub is down")),
        )

        with caplog.at_level("WARNING", logger="shannon.services.access"):
            assert await refusal(service) is None

        assert "Discord role decides" in caplog.text


class TestTheOneWayItSaysNo:
    async def test_no_access_at_all_is_refused(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        service = access(db_sessionmaker, permissions=FakePermissions(people.NO_ACCESS))

        said = await refusal(service)

        assert said is not None
        assert "not have monalisa as a collaborator" in said

    async def test_read_only_is_refused_and_told_apart_from_no_access(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Two sentences, because they are two different situations and one of them is fixed by
        asking for access while the other is fixed by asking for MORE access."""
        service = access(db_sessionmaker, permissions=FakePermissions(people.READ_ONLY))

        said = await refusal(service)

        assert said is not None
        assert "can read" in said
        assert "cannot write" in said

    async def test_the_read_refusal_explains_the_triage_fold(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Somebody with triage sees "read" and thinks the bot got it wrong. GitHub folds triage
        onto read before it answers, so the sentence says whose rule it is."""
        service = access(db_sessionmaker, permissions=FakePermissions(people.READ_ONLY))

        said = await refusal(service)

        assert said is not None
        assert "Triage counts as read" in said

    async def test_both_refusals_name_the_account(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """What makes them actionable. Somebody may be signed in as an account they forgot they
        proved, or one they have since renamed on GitHub - which reads here as an account that
        is not a collaborator, because a login is asked about by name."""
        for permission in (people.NO_ACCESS, people.READ_ONLY):
            said = await refusal(access(db_sessionmaker, permissions=FakePermissions(permission)))

            assert said is not None
            assert LOGIN in said

    async def test_the_no_access_refusal_says_to_link_again(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """A renamed GitHub account is the case that looks like no access and is not. It is
        self-healing, but only if the refusal says how."""
        service = access(db_sessionmaker, permissions=FakePermissions(people.NO_ACCESS))

        said = await refusal(service)

        assert said is not None
        assert "/link" in said

    async def test_it_asks_about_the_proved_login_on_the_registered_repository(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        permissions = FakePermissions()
        await refusal(access(db_sessionmaker, permissions=permissions))

        assert permissions.asked == [("Canon-Regularis", "Shannon-bot", LOGIN)]


class TestTheLadder:
    @pytest.mark.parametrize(
        ("permission", "wanted", "allowed"),
        [
            (people.ADMIN, people.WRITE, True),
            (people.WRITE, people.WRITE, True),
            (people.READ_ONLY, people.WRITE, False),
            (people.NO_ACCESS, people.WRITE, False),
            (people.READ_ONLY, people.READ_ONLY, True),
            (people.ADMIN, people.ADMIN, True),
            (people.WRITE, people.ADMIN, False),
        ],
    )
    def test_what_reaches_what(self, permission: str, wanted: str, allowed: bool) -> None:
        assert people.at_least(permission, wanted) is allowed

    def test_a_word_github_has_never_sent_reaches_nothing(self) -> None:
        """These are wire values off a JSON body, not a column this bot controls. A fifth name
        appearing one day should refuse a write rather than end the command in a traceback."""
        assert people.at_least("superuser", people.READ_ONLY) is False


class TestWhenTheServerInsistsOnAProvedAccount:
    """`SHANNON_REQUIRE_PROVED_LINKS`, which until now only reached `/assign`.

    The hole it closes is the widest one here: an unproved caller was let through on their Discord
    role alone, so a server could hold every GitHub permission check it liked and a member who had
    never run `/link` went past all of them. Off by default, because turning it on breaks every
    server whose members have not linked - which is the same reason it was a hole rather than a bug.
    """

    async def test_an_unproved_caller_is_refused_and_told_what_to_run(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        service = access(db_sessionmaker, proof=FakeProof(login=None), require_proved=True)

        said = await refusal(service)

        assert said is not None
        assert "/link" in said, "it refused without saying how to satisfy it"

    async def test_a_proved_caller_is_unaffected(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """The setting is about callers with no proof. Somebody who has one is judged on their
        GitHub permission exactly as before."""
        service = access(db_sessionmaker, require_proved=True)

        assert await refusal(service) is None

    async def test_a_deployment_with_no_app_sign_in_still_lets_them_through(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """The half of the condition that is not optional. With no public URL there is no way to
        produce a proof, so refusing for the lack of one would take these eight commands away from
        everybody for ever, with the only remedy a setting nobody would connect to the symptom.

        `/assign` pairs the same two for the same reason. A deployment missing only Discord is not
        this case: see the test below.
        """
        service = access(
            db_sessionmaker,
            proof=FakeProof(login=None, configured=False),
            require_proved=True,
        )

        assert await refusal(service) is None

    async def test_a_deployment_missing_only_discord_still_refuses(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """Found reviewing #201's fix. Without Discord nobody can prove anything either, and
        this still enforces, on purpose: `/link` refuses there naming the two settings it lacks,
        so the remedy is one command from the symptom, and leaving Discord unset must not be a way
        to switch the setting off. Against the real service, because what is pinned is which of
        its properties the gate reads."""
        async with httpx.AsyncClient() as http:
            verification = without_discord(db_sessionmaker, http)
            service = GitHubAccess(db_sessionmaker, FakePermissions(), verification, True)

            said = await refusal(service)

        assert verification.can_prove_identity is False
        assert said is not None
        assert "/link" in said

    async def test_off_by_default_an_unproved_caller_passes(
        self, db_sessionmaker: async_sessionmaker[AsyncSession], registered: Repository
    ) -> None:
        """What every existing deployment does, and the behaviour this must not change silently."""
        service = access(db_sessionmaker, proof=FakeProof(login=None))

        assert await refusal(service) is None
