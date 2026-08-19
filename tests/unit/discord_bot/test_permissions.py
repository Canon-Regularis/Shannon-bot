from __future__ import annotations

import pytest

from shannon.commands._permissions import REGISTER_ROLES, SYNC_ROLES
from shannon.config import Settings
from shannon.discord_bot.permissions import PermissionGate
from shannon.domain.enums import CommandRole
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole


@pytest.fixture
def gate() -> PermissionGate:
    return PermissionGate(Settings())


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


def test_admins_can_register(gate: PermissionGate) -> None:
    assert gate.allows(member(administrator=True), REGISTER_ROLES) is True


def test_project_managers_can_register(gate: PermissionGate) -> None:
    assert gate.allows(member("Project Manager"), REGISTER_ROLES) is True


def test_developers_cannot_register(gate: PermissionGate) -> None:
    assert gate.allows(member("Developer"), REGISTER_ROLES) is False


def test_reviewers_cannot_register(gate: PermissionGate) -> None:
    assert gate.allows(member("Reviewer"), REGISTER_ROLES) is False


@pytest.mark.parametrize("role_name", ["Developer", "Project Manager"])
def test_the_tiers_the_table_grants_can_sync(gate: PermissionGate, role_name: str) -> None:
    assert gate.allows(member(role_name), SYNC_ROLES) is True


def test_a_reviewer_alone_cannot_sync(gate: PermissionGate) -> None:
    """The permissions table grants /pr and /issue to developers and project managers only."""
    assert gate.allows(member("Reviewer"), SYNC_ROLES) is False


@pytest.mark.parametrize("also", ["Developer", "Project Manager"])
def test_a_reviewer_who_is_also_something_else_keeps_that(gate: PermissionGate, also: str) -> None:
    """Holding any listed role grants a command. Roles add up rather than ranking each other."""
    assert gate.allows(member("Reviewer", also), SYNC_ROLES) is True


def test_holding_every_role_at_once_grants_everything(gate: PermissionGate) -> None:
    everything = member("Reviewer", "Developer", "Project Manager")

    assert gate.allows(everything, SYNC_ROLES) is True
    assert gate.allows(everything, REGISTER_ROLES) is True


def test_admins_pass_every_gate(gate: PermissionGate) -> None:
    admin = member(administrator=True)

    assert gate.allows(admin, SYNC_ROLES) is True
    assert gate.allows(admin, REGISTER_ROLES) is True


def test_a_member_with_no_roles_is_rejected(gate: PermissionGate) -> None:
    assert gate.allows(member(), SYNC_ROLES) is False
    assert gate.allows(member(), REGISTER_ROLES) is False


def test_custom_role_names_from_configuration() -> None:
    gate = PermissionGate(Settings(role_reviewer="Code Owners, Maintainers"))

    assert gate.roles_of(member("Maintainers")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Code Owners")) == {CommandRole.REVIEWER}
    assert gate.roles_of(member("Reviewer")) == frozenset()


def test_denial_message_lists_the_roles_that_would_work(gate: PermissionGate) -> None:
    message = gate.denial("register", REGISTER_ROLES)

    assert "/register" in message
    assert "Admin" in message
    assert "Project Manager" in message


def test_denial_message_uses_configured_names() -> None:
    gate = PermissionGate(Settings(role_project_manager="Leads"))

    assert "Leads" in gate.denial("register", REGISTER_ROLES)


def test_an_object_without_discord_attributes_is_rejected(gate: PermissionGate) -> None:
    assert gate.allows(object(), REGISTER_ROLES) is False
