"""Who may run which command.

The gate's own mechanics are tested beside the gate, in tests/unit/discord_bot. This file is
about the table in shannon/commands/_permissions.py: which tiers each command is open to, and
what somebody without one is told.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from discord import app_commands

import shannon.commands
from shannon.commands._permissions import REGISTER_ROLES, SYNC_ROLES, UNGATED
from shannon.config import Settings
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.roles import ConfiguredRoles
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole


def member(*role_names: str, administrator: bool = False) -> FakeMember:
    return FakeMember(
        roles=[FakeRole(name) for name in role_names],
        guild_permissions=FakeGuildPermissions(administrator=administrator),
    )


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


def test_denial_message_lists_the_roles_that_would_work(gate: PermissionGate) -> None:
    message = gate.denial("register", REGISTER_ROLES)

    assert "/register" in message
    assert "Admin" in message
    assert "Project Manager" in message


def test_denial_message_uses_configured_names() -> None:
    gate = PermissionGate(ConfiguredRoles.from_settings(Settings(role_project_manager="Leads")))

    assert "Leads" in gate.denial("register", REGISTER_ROLES)


def test_denial_message_with_every_tier_blanked_names_nothing() -> None:
    """Emptying a role setting is how a server turns a tier off, and it leaves nothing to list.

    The message has to stop rather than trail off after the colon. A guild administrator still
    gets through: that is read off Discord's own permission bit, not off a configured name.
    """
    gate = PermissionGate(
        ConfiguredRoles.from_settings(Settings(role_admin="", role_project_manager=" , "))
    )

    assert gate.denial("register", REGISTER_ROLES) == "You are not allowed to use /register."


def every_command_factory() -> dict[str, object]:
    """Every `build_*_command` in the commands package, by the name Discord will show.

    Walked rather than listed, because a list is the thing that drifts. A factory building
    several commands answers for each of them.
    """
    found: dict[str, object] = {}
    for module in pkgutil.iter_modules(shannon.commands.__path__):
        imported = importlib.import_module(f"shannon.commands.{module.name}")
        for name, value in vars(imported).items():
            if not (name.startswith("build_") and name.endswith(("_command", "_commands"))):
                continue
            if not callable(value) or inspect.isclass(value):
                continue
            found[name] = value
    return found


def commands_from(factory: object) -> list[str]:
    """The Discord names a factory produces, taking whatever stubs its arguments need."""
    parameters = inspect.signature(factory).parameters
    built = factory(*(object() for _ in parameters))
    made = built if isinstance(built, tuple) else (built,)
    return [command.name for command in made if isinstance(command, app_commands.Command)]


def test_the_commands_that_take_no_gate_are_the_ones_named() -> None:
    """Every command factory takes a permission gate except the ones written down.

    A gate dropped from a factory by accident has no symptom anybody would notice: the command
    still builds, still registers with Discord and still works. It just works for everybody.
    Nothing else in the suite looks at whether a gate is there, only at what it answers, so a
    factory that stopped asking for one would go on passing.

    Read off the signatures rather than off a second list, so the two cannot drift. Adding a
    command anybody may run means editing `_permissions.UNGATED`, which is a sentence somebody
    has to mean.
    """
    ungated = {
        name
        for factory in every_command_factory().values()
        if "gate" not in inspect.signature(factory).parameters
        for name in commands_from(factory)
    }

    assert ungated == set(UNGATED)
