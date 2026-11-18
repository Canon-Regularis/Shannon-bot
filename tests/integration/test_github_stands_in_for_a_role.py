"""Whether GitHub vouches for a member the Discord role turned away. Issue #94.

`GitHubTierGrants` against a real database, so the proof and the repository it judges on are the
rows the round trip and `/register` leave, `through_discord` and all. GitHub is the fake, stocked
per test: it answers no access and no account unless told otherwise, because a grant must never
be the default.

Most of what is tested here is the ways it says NO, the reverse of `test_github_decides_access.py`
beside it. That one only restricts, so it fails open; this one raises access, so every answer it
cannot stand behind - no proof, an old proof, a name that changed hands, GitHub not answering - is
a refusal, and each says which.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shannon.db.models import Repository, VerifiedIdentity
from shannon.db.stores.identities import VerifiedIdentityStore
from shannon.github import people
from shannon.github.errors import GitHubRateLimitError, GitHubUnavailableError
from shannon.github.people import Standing
from shannon.services.tier_grants import GitHubTierGrants
from tests.fakes.github import FakeGitHubClient
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.integration

GUILD = 1
WHO = 424242
LOGIN = "monalisa"
ACCOUNT = 583231
REPO = f"{payloads.OWNER}/{payloads.REPO}"
PROVED_AT = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
LOGGER = "shannon.services.tier_grants"


class Clock:
    """Seconds that pass only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def github() -> FakeGitHubClient:
    return FakeGitHubClient()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def grants(
    db_sessionmaker: async_sessionmaker[AsyncSession], github: FakeGitHubClient, clock: Clock
) -> GitHubTierGrants:
    return GitHubTierGrants(db_sessionmaker, github, clock=clock)


async def prove(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    guild_id: int = GUILD,
    discord_user_id: int = WHO,
    at: datetime = PROVED_AT,
) -> None:
    """Prove `LOGIN` for a member the way the round trip does, which is through Discord."""
    async with sessionmaker() as session, session.begin():
        await VerifiedIdentityStore(session).remember(
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            github_login=LOGIN,
            github_user_id=ACCOUNT,
            verified_at=at,
        )


async def proved_before_0039(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """A proof as migration `0039` left one it found, with the column at its default."""
    async with sessionmaker() as session, session.begin():
        session.add(
            VerifiedIdentity(
                discord_guild_id=GUILD,
                discord_user_id=WHO,
                github_login=LOGIN,
                github_user_id=ACCOUNT,
                verified_at=PROVED_AT,
            )
        )


def stands(github: FakeGitHubClient, permission: str, account_id: int | None = ACCOUNT) -> None:
    github.standings[LOGIN] = Standing(permission, account_id)


async def refusal(grants: GitHubTierGrants, *, level: str = people.WRITE) -> str | None:
    return await grants.refusal_for(guild_id=GUILD, discord_user_id=WHO, level=level)


def would(level: str = people.WRITE) -> str:
    return f"GitHub's {level} permission on {REPO} would let you in as well"


class TestTheWaysItGrants:
    async def test_write_passes_a_developer_gate(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        stands(github, people.WRITE)

        assert await refusal(grants) is None

    async def test_admin_passes_a_developer_gate_and_a_project_manager_gate(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """The ladder rather than an equality, as in #158: refusing an admin a write gate is the
        bug a `==` would ship."""
        await prove(db_sessionmaker)
        stands(github, people.ADMIN)

        assert await refusal(grants) is None
        assert await refusal(grants, level=people.ADMIN) is None

    async def test_it_asks_about_the_proved_login_on_the_registered_repository(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        stands(github, people.WRITE)

        await refusal(grants)

        assert github.standing_calls == [(REPO.lower(), LOGIN)]

    async def test_a_grant_is_written_down_with_everything_that_decided_it(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The audit line. Somebody let in by GitHub rather than by a role is the case somebody
        will one day want to find again, so it names the member, the account, the permission,
        the repository and the level."""
        await prove(db_sessionmaker)
        stands(github, people.ADMIN)

        with caplog.at_level("INFO", logger=LOGGER):
            await refusal(grants)

        assert caplog.messages == [
            f"GitHub's admin permission for {LOGIN} on {REPO} let Discord member {WHO} in at "
            "the write level"
        ]


class TestTheWaysItGrantsNothing:
    """Each with its own sentence, because each is fixed a different way."""

    async def test_a_server_with_nothing_registered_asks_nobody(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        stands(github, people.ADMIN)

        said = await refusal(grants, level=people.ADMIN)

        assert said == (
            "GitHub's admin permission on this server's repository would let you in as well, "
            "once one is registered."
        )
        assert github.standing_calls == []

    async def test_a_member_who_never_proved_an_account_is_told_to_link(
        self, registered: Repository, github: FakeGitHubClient, grants: GitHubTierGrants
    ) -> None:
        """The opposite of #158's fallback. With no account to ask about there is nothing GitHub
        can vouch for, so the role stands alone - and that is a refusal here, not a pass."""
        said = await refusal(grants)

        assert said == f"{would()}, once /link has proved which GitHub account is yours."
        assert github.standing_calls == []

    async def test_a_proof_from_before_0039_does_not_count(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Before migration `0033` a forwarded link signed in whoever clicked it, so a row from
        then may name somebody else's account. Nothing on the row tells those apart from honest
        ones, so all of them need one more `/link`, and GitHub is not asked on their word."""
        await proved_before_0039(db_sessionmaker)
        stands(github, people.ADMIN)

        said = await refusal(grants)

        assert said == (
            f"{would()}, but your proof of {LOGIN} was made before links went through Discord, "
            "so run /link once more for it to count."
        )
        assert github.standing_calls == []

    async def test_one_more_link_makes_an_old_proof_count(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await proved_before_0039(db_sessionmaker)
        await prove(db_sessionmaker, at=PROVED_AT + timedelta(days=1))
        stands(github, people.WRITE)

        assert await refusal(grants) is None

    async def test_a_proof_in_another_server_counts_for_nothing_here(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker, guild_id=GUILD + 1)
        stands(github, people.ADMIN)

        said = await refusal(grants)

        assert said == f"{would()}, once /link has proved which GitHub account is yours."
        assert github.standing_calls == []

    async def test_read_says_so_and_explains_the_triage_fold(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Somebody with triage sees "read" and thinks the bot got it wrong. GitHub folds triage
        onto read before it answers, so the sentence says whose rule it is."""
        await prove(db_sessionmaker)
        stands(github, people.READ_ONLY)

        said = await refusal(grants)

        assert said == (
            f"{would()}, but GitHub gives {LOGIN} read access there. Triage counts as read here, "
            "which is GitHub's own folding."
        )

    async def test_no_access_says_github_does_not_have_them(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        stands(github, people.NO_ACCESS)

        said = await refusal(grants)

        assert said == (
            f"{would()}, but GitHub does not have {LOGIN} as a collaborator there. Run /link "
            "again if that name is no longer yours."
        )

    async def test_a_name_github_has_never_heard_of_reads_as_no_access_rather_than_a_handover(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """A 404 names no account, which would otherwise read as a different one. Saying the name
        changed hands there would be a guess; saying GitHub does not have them is what it said."""
        await prove(db_sessionmaker)
        stands(github, people.NO_ACCESS, None)

        said = await refusal(grants)

        assert said is not None
        assert "does not have monalisa as a collaborator" in said

    async def test_write_does_not_pass_a_project_manager_gate(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """ "GitHub's admin permission", never "Admin", so it cannot be read as the Discord role the
        denial in front of it may have just named."""
        await prove(db_sessionmaker)
        stands(github, people.WRITE)

        said = await refusal(grants, level=people.ADMIN)

        assert said == f"{would(people.ADMIN)}, but GitHub gives {LOGIN} write access there."

    async def test_a_word_github_has_never_sent_grants_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Wire values off a JSON body, not a column this bot controls: a fifth name appearing one
        day must refuse rather than let somebody in."""
        await prove(db_sessionmaker)
        stands(github, "superuser")

        said = await refusal(grants)

        assert said == f"{would()}, but GitHub gives {LOGIN} superuser access there."

    async def test_a_login_that_now_belongs_to_somebody_else_grants_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """The case the ids are compared for. GitHub hands a freed login to whoever claims it, so
        asked by name it answers about them, and their admin is not the member's to borrow."""
        await prove(db_sessionmaker)
        stands(github, people.ADMIN, ACCOUNT + 1)

        said = await refusal(grants)

        assert said == (
            f"{would()}, but the name {LOGIN} now belongs to a different account from the one "
            "you proved, so run /link again."
        )

    async def test_an_answer_that_names_no_account_grants_nothing(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """No id is not a match. The client answers None for a body without one, and a grant
        has to know whose permission it is granting on."""
        await prove(db_sessionmaker)
        stands(github, people.ADMIN, None)

        said = await refusal(grants)

        assert said == (
            f"{would()}, but GitHub did not say which account {LOGIN} is, so its answer could "
            "not be held against the one you proved."
        )


class TestFailingClosed:
    """GitHub not answering is no answer, so it is raised for the caller to refuse on.

    The opposite of `test_github_decides_access.py`, where an outage lets the role decide alone.
    That check only ever restricts somebody the role already let in; this one lets somebody in,
    and doing that because GitHub was down would be a door anybody can open by waiting.
    """

    @pytest.mark.parametrize(
        "error",
        [GitHubUnavailableError("GitHub is down"), GitHubRateLimitError("Slow down")],
        ids=["outage", "rate-limit"],
    )
    async def test_github_not_answering_is_raised_and_logged(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        await prove(db_sessionmaker)
        github.standing_error = error

        with caplog.at_level("WARNING", logger=LOGGER), pytest.raises(type(error)):
            await refusal(grants)

        assert "only the Discord role counts" in caplog.text
        assert LOGIN in caplog.text

    async def test_github_not_answering_is_not_remembered(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """So the first ask once GitHub is back is a real one."""
        await prove(db_sessionmaker)
        github.standing_error = GitHubUnavailableError("GitHub is down")
        with pytest.raises(GitHubUnavailableError):
            await refusal(grants)

        github.standing_error = None
        stands(github, people.WRITE)

        assert await refusal(grants) is None
        assert len(github.standing_calls) == 2


class TestARefusalRememberedForAMinute:
    """A member with no role could otherwise spend the installation's GitHub budget - the one the
    webhook sync and the board poller run on - one refused command at a time. Only answers that
    grant nothing are kept, so access taken away on GitHub is taken away here at once."""

    async def test_asking_again_within_the_minute_asks_nobody(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
        clock: Clock,
    ) -> None:
        await prove(db_sessionmaker)
        first = await refusal(grants)
        clock.now += 59.9

        again = await refusal(grants)

        assert again == first
        assert len(github.standing_calls) == 1

    async def test_a_remembered_answer_is_worded_for_the_level_asked_now(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        await refusal(grants)

        said = await refusal(grants, level=people.ADMIN)

        assert said is not None
        assert said.startswith(would(people.ADMIN))
        assert len(github.standing_calls) == 1

    async def test_after_the_minute_github_is_asked_again(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
        clock: Clock,
    ) -> None:
        """Somebody just given write on GitHub waits a minute at most."""
        await prove(db_sessionmaker)
        await refusal(grants)
        stands(github, people.WRITE)
        clock.now += 60

        assert await refusal(grants) is None
        assert len(github.standing_calls) == 2

    async def test_proving_again_asks_again_at_once(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Running /link again is what a refusal tells a member to do, so it has to work now
        rather than in a minute. A new proof has a new `verified_at`, which nothing was kept
        under."""
        await prove(db_sessionmaker)
        await refusal(grants)
        await prove(db_sessionmaker, at=PROVED_AT + timedelta(minutes=5))
        stands(github, people.WRITE)

        assert await refusal(grants) is None
        assert len(github.standing_calls) == 2

    async def test_one_member_s_refusal_is_not_another_s(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        await prove(db_sessionmaker)
        await prove(db_sessionmaker, discord_user_id=WHO + 1)
        await refusal(grants)

        await grants.refusal_for(guild_id=GUILD, discord_user_id=WHO + 1, level=people.WRITE)

        assert len(github.standing_calls) == 2

    async def test_an_answer_that_grants_is_never_remembered(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Write taken away on GitHub is taken away here on the very next command."""
        await prove(db_sessionmaker)
        stands(github, people.WRITE)
        assert await refusal(grants) is None
        stands(github, people.NO_ACCESS)

        assert await refusal(grants) is not None
        assert len(github.standing_calls) == 2

    async def test_write_refused_at_admin_is_not_remembered(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Write is a refusal at a Project Manager gate and a grant at a Developer one, so keeping
        it would be keeping a grant."""
        await prove(db_sessionmaker)
        stands(github, people.WRITE)
        assert await refusal(grants, level=people.ADMIN) is not None
        stands(github, people.NO_ACCESS)

        assert await refusal(grants) is not None
        assert len(github.standing_calls) == 2

    async def test_a_name_that_changed_hands_is_remembered_as_a_refusal(
        self,
        db_sessionmaker: async_sessionmaker[AsyncSession],
        registered: Repository,
        github: FakeGitHubClient,
        grants: GitHubTierGrants,
    ) -> None:
        """Whatever the other account may do, none of it is the member's."""
        await prove(db_sessionmaker)
        stands(github, people.ADMIN, ACCOUNT + 1)
        await refusal(grants)

        said = await refusal(grants)

        assert said is not None
        assert "now belongs to a different account" in said
        assert len(github.standing_calls) == 1
