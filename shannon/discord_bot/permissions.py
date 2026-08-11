from __future__ import annotations

from collections.abc import Collection

from shannon.config import Settings
from shannon.domain.enums import CommandRole

REGISTER_ROLES = frozenset({CommandRole.ADMIN, CommandRole.PROJECT_MANAGER})
SYNC_ROLES = frozenset({CommandRole.DEVELOPER, CommandRole.REVIEWER, CommandRole.PROJECT_MANAGER})


class PermissionGate:
    """Turns a member's Discord roles into the permission tiers they hold.

    Role names come from configuration, so a server can call its reviewers whatever it likes
    without a code change.

    Members are read with getattr rather than against a typed protocol, so an object that is
    not a guild member at all resolves to no permissions instead of raising.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def roles_of(self, member: object) -> frozenset[CommandRole]:
        held: set[CommandRole] = set()

        # A guild administrator outranks every configured role, including a server that never
        # set the role names up at all.
        if getattr(getattr(member, "guild_permissions", None), "administrator", False):
            held.add(CommandRole.ADMIN)

        names = {
            role.name.strip().lower()
            for role in getattr(member, "roles", ())
            if isinstance(getattr(role, "name", None), str)
        }
        for role in CommandRole:
            if names & self._settings.role_names(role):
                held.add(role)

        return frozenset(held)

    def allows(self, member: object, allowed: Collection[CommandRole]) -> bool:
        held = self.roles_of(member)
        if CommandRole.ADMIN in held:
            return True
        return bool(held & set(allowed))

    def denial(self, command: str, allowed: Collection[CommandRole]) -> str:
        names = [
            name for role in sorted(allowed) for name in self._settings.role_display_names(role)
        ]
        if not names:
            return f"You are not allowed to use /{command}."
        return f"You need one of these roles to use /{command}: {', '.join(names)}."
