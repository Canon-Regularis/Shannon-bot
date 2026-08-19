from __future__ import annotations

import pytest

from shannon.commands._permissions import REGISTER_ROLES
from shannon.config import Settings
from shannon.discord_bot.permissions import PermissionGate
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
