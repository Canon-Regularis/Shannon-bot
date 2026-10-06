from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from shannon.discord_bot.roles import CommandRole
from shannon.discord_bot.threads import FindsMembers


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


class MemberTiers:
    """Whether a member holds a tier in a server now, asked of Discord rather than remembered.

    Found reviewing #201. A slash command is gated on the roles Discord sends with it, but a board
    link is followed up to ten minutes later, from a browser, with no interaction to read roles
    off. So this fetches the member and puts them through the same gate the command used, which is
    what makes the second answer comparable with the first.
    """

    def __init__(self, members: FindsMembers, gate: PermissionGate) -> None:
        self._members = members
        self._gate = gate

    async def holds(
        self, *, guild_id: int, discord_user_id: int, tiers: Collection[CommandRole]
    ) -> bool:
        """Any of `tiers`, as the command's gate reads them: an administrator holds every one.

        Somebody no longer in the server holds none. Where Discord cannot be asked this raises
        `DiscordGatewayError`, which is a different answer and the caller's to refuse on.
        """
        member = await self._members.member(guild_id=guild_id, user_id=discord_user_id)
        return member is not None and self._gate.allows(member, tiers)
