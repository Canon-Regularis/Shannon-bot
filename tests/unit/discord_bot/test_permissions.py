from __future__ import annotations

import pytest

from shannon.commands._permissions import REGISTER_ROLES
from shannon.config import Settings
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.permissions import MemberTiers, PermissionGate
from shannon.discord_bot.roles import CommandRole, ConfiguredRoles
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole


@pytest.fixture
def gate() -> PermissionGate:
    return PermissionGate(ConfiguredRoles.from_settings(Settings()))


def member(*role_names: str, administrator: bool = False) -> FakeMember:
    return FakeMember(
        roles=[FakeRole(name) for name in role_names],
        guild_permissions=FakeGuildPermissions(administrator=administrator),
    )


def test_guild_administrator_holds_admin(gate: PermissionGate) -> None:
    assert CommandRole.ADMIN in gate.roles_of(member(administrator=True))


def test_configured_role_names_map_to_tiers(gate: PermissionGate) -> None:
    held = gate.roles_of(member("Reviewer", "Developer"))

    assert held == {CommandRole.REVIEWER, CommandRole.DEVELOPER}


def test_role_matching_ignores_case_and_padding(gate: PermissionGate) -> None:
    assert gate.roles_of(member("  pROJECT manager  ")) == {CommandRole.PROJECT_MANAGER}


def test_unrelated_roles_grant_nothing(gate: PermissionGate) -> None:
    assert gate.roles_of(member("Bots", "Gamers")) == frozenset()


def test_custom_role_names_from_configuration() -> None:
    gate = PermissionGate(
        ConfiguredRoles.from_settings(Settings(role_reviewer="Code Owners, Maintainers"))
    )

    assert gate.roles_of(member("Maintainers")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Code Owners")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Reviewer")) == frozenset()


def test_an_object_without_discord_attributes_is_rejected(gate: PermissionGate) -> None:
    assert gate.allows(object(), REGISTER_ROLES) is False


class Members:
    """Who is in the server by id, or a Discord that will not say, and who was asked about."""

    def __init__(self, *known: tuple[int, FakeMember], fails: Exception | None = None) -> None:
        self.known = dict(known)
        self.fails = fails
        self.asked: list[tuple[int, int]] = []

    async def member(self, *, guild_id: int, user_id: int) -> object | None:
        self.asked.append((guild_id, user_id))
        if self.fails is not None:
            raise self.fails
        return self.known.get(user_id)


class TestAskingDiscordWhetherAMemberStillHoldsATier:
    """`MemberTiers`, asked when a board link is followed. Found reviewing #201. It puts the member
    Discord has now through the same gate the command used, so the two answers compare."""

    async def test_a_member_still_holding_the_tier_holds_it(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Project Manager"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is True

    async def test_a_member_who_lost_it_does_not(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Developer"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False

    async def test_somebody_no_longer_in_the_server_holds_nothing(
        self, gate: PermissionGate
    ) -> None:
        tiers = MemberTiers(Members(), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES) is False

    async def test_an_administrator_holds_every_tier_even_none(self, gate: PermissionGate) -> None:
        """Including the empty set, which is what a tier this code no longer knows narrows to."""
        tiers = MemberTiers(Members((555, member(administrator=True))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=frozenset()) is True

    async def test_the_empty_set_is_administrators_only(self, gate: PermissionGate) -> None:
        tiers = MemberTiers(Members((555, member("Project Manager"))), gate)

        assert await tiers.holds(guild_id=1, discord_user_id=555, tiers=frozenset()) is False

    async def test_discord_not_answering_is_not_an_answer(self, gate: PermissionGate) -> None:
        """Raised rather than read as no, so the caller can say which it was."""
        tiers = MemberTiers(Members(fails=DiscordGatewayError("Discord is down")), gate)

        with pytest.raises(DiscordGatewayError):
            await tiers.holds(guild_id=1, discord_user_id=555, tiers=REGISTER_ROLES)

    async def test_it_asks_about_the_member_in_the_server_named(self, gate: PermissionGate) -> None:
        members = Members()

        await MemberTiers(members, gate).holds(
            guild_id=7, discord_user_id=555, tiers=REGISTER_ROLES
        )

        assert members.asked == [(7, 555)]
