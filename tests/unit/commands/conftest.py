"""What every command test needs: a gate, and members holding a role.

Each of the four command test files built these from scratch, and the member builder had four
spellings between them.
"""

from __future__ import annotations

import pytest

from shannon.config import Settings
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.roles import ConfiguredRoles
from tests.fakes.discord_objects import FakeGuildPermissions, FakeMember, FakeRole


def default_gate() -> PermissionGate:
    """The real gate on default role names, which is what production ships.

    A plain function rather than a fixture because the command builders below are called from
    module-level helpers, which cannot ask for one.
    """
    return PermissionGate(ConfiguredRoles.from_settings(Settings()))


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
