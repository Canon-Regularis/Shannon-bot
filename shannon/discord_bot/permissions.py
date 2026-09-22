from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from shannon.discord_bot.roles import CommandRole


class RoleNames(Protocol):
    """The role names a server has configured, which is all a permission check needs.

    The whole of Settings would hand it a database URL and a bot token as well.
    """

    def role_names(self, role: CommandRole) -> frozenset[str]: ...

    def role_display_names(self, role: CommandRole) -> tuple[str, ...]: ...


class PermissionGate:
    """Turns a member's Discord roles into the permission tiers they hold.

    Members are read with getattr rather than against a typed protocol, so an object that is not
    a guild member at all resolves to no permissions instead of raising.
    """

    def __init__(self, settings: RoleNames) -> None:
        self._settings = settings

    def roles_of(self, member: object) -> frozenset[CommandRole]:
        held: set[CommandRole] = set()

        # A guild administrator outranks every configured role, even where none are set.
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
        """Whether a member holds any of the tiers a command is open to; any, not all."""
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
