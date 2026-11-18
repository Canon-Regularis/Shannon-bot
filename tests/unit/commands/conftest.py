"""What every command test needs: a gate, members holding a role, and a GitHub proof.

Each of the four command test files built these from scratch, and the member builder had four
spellings between them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shannon.config import Settings
from shannon.db.stores.identities import ProvedAccount
from shannon.discord_bot.permissions import GrantsTiers, PermissionGate
from shannon.discord_bot.roles import ConfiguredRoles
from shannon.domain.enums import VerificationPurpose
from shannon.github import people
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole


class FakeVerification:
    """A deployment that can ask GitHub who somebody is, and a person who has just been asked.

    Shared since issue #135, when `/register` grew the same two-run shape `/unregister` has. The
    defaults are the happy path on purpose: every test that is not about proving anything then
    reads as though the proof were not there.
    """

    def __init__(self, *, can_prove_identity: bool = True, proved: str | None = "octocat") -> None:
        self.can_prove_identity = can_prove_identity
        # Still a login, because that is what every test here is about saying. The account it
        # stands for is built below, so no test has to name an id it does not care about.
        self.proved = proved
        self.links_handed_out = 0
        self.purposes: list[VerificationPurpose] = []

    async def proved_just_now(self, *, guild_id: int, discord_user_id: int) -> ProvedAccount | None:
        if self.proved is None:
            return None
        return ProvedAccount(
            login=self.proved, github_user_id=583231, verified_at=datetime(2026, 9, 17, tzinfo=UTC)
        )

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str:
        self.links_handed_out += 1
        self.purposes.append(purpose)
        return "https://shannon.example.com/oauth/start?state=abc"


def default_gate() -> PermissionGate:
    """The real gate on default role names, which is what production ships.

    A plain function rather than a fixture because the command builders below are called from
    module-level helpers, which cannot ask for one.
    """
    return PermissionGate(ConfiguredRoles.from_settings(Settings()))


def gate_with(grants: GrantsTiers) -> PermissionGate:
    """The same gate on a server that lets GitHub stand in for a role. Issue #94.

    `default_gate` is a server that never turned that on, which is every deployment's default and
    the reason every refusal test written before it still reads as it did.
    """
    return PermissionGate(ConfiguredRoles.from_settings(Settings()), grants)


class GitHubGives:
    """GitHub answering with one permission, at whatever level a gate asks it about. Issue #94.

    `FakeGrants` is told its answer outright, which is all a guard needs. A test driving a real
    command needs the level that command's own gate asks at to be what decides, so this works the
    answer out the way the real grant does once a proof and a repository are in hand.
    """

    def __init__(self, permission: str) -> None:
        self.permission = permission
        self.asked: list[str] = []

    async def refusal_for(self, *, guild_id: int, discord_user_id: int, level: str) -> str | None:
        self.asked.append(level)
        if people.at_least(self.permission, level):
            return None
        return f"GitHub's {level} permission would let you in as well."


@pytest.fixture
def gate() -> PermissionGate:
    return default_gate()


def member_with(role: str) -> FakeMember:
    return FakeMember(roles=[FakeRole(role)])


def project_manager() -> FakeMember:
    return member_with("Project Manager")


def developer() -> FakeMember:
    return member_with("Developer")


def administrator() -> FakeMember:
    """Outranks every configured role, including on a server that never set them up."""
    return FakeMember(guild_permissions=FakeGuildPermissions(administrator=True))


class FakeAccess:
    """Whether GitHub would allow the caller, stood in for.

    `None` is what a caller who has never proved an account gets, which is the default here for
    the same reason it is the default in the service: it is the behaviour every command had
    before the gate existed, so a test that says nothing about GitHub gets the old answer.
    """

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.asked: list[tuple[int, int, str]] = []

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, at_least: str
    ) -> str | None:
        self.asked.append((guild_id, discord_user_id, at_least))
        return self.refusal
