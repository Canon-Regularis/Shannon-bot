from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, Protocol

from shannon.discord_bot.roles import CommandRole
from shannon.discord_bot.threads import FindsMembers
from shannon.github import people


class RoleNames(Protocol):
    """The role names a server has configured, which is all a permission check needs.

    The whole of Settings would hand it a database URL and a bot token as well.
    """

    def role_names(self, role: CommandRole) -> frozenset[str]: ...

    def role_display_names(self, role: CommandRole) -> tuple[str, ...]: ...


class GrantsTiers(Protocol):
    """Whether GitHub vouches for a member at a permission level, where the role did not.

    Issue #94. None means GitHub's answer reaches `level` for the account the member proved, and
    a string is the sentence that follows the role's own denial. Where GitHub could not be asked
    this raises `GitHubError` rather than answering, and every caller reads that as a refusal:
    this raises access, so not knowing has to mean no.
    """

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, level: str
    ) -> str | None: ...


# Which tier GitHub can stand in for, and the permission it takes, lowest first so the first that
# fits is the cheapest a member can be let in on. Issue #94. Write is a Developer and admin is a
# Project Manager as well. The Admin tier is absent on purpose: it passes every gate in `allows`,
# so letting GitHub stand in for it would let GitHub decide every command there is, and that
# stays a decision Discord makes. Reviewer is absent because it grants no command at all.
_STANDS_IN: Final = (
    (CommandRole.DEVELOPER, people.WRITE),
    (CommandRole.PROJECT_MANAGER, people.ADMIN),
)


@dataclass(frozen=True, slots=True)
class GitHubRoute:
    """The one question left to ask GitHub about a member the role turned away.

    The level is bound in rather than passed at the call, so a caller holding a route always has
    something to ask and never has to decide what GitHub permission a command is worth.
    """

    level: str
    grants: GrantsTiers

    async def refusal_for(self, *, guild_id: int, discord_user_id: int) -> str | None:
        """What `GrantsTiers.refusal_for` answers at this route's level, raising as it does."""
        return await self.grants.refusal_for(
            guild_id=guild_id, discord_user_id=discord_user_id, level=self.level
        )


class PermissionGate:
    """Turns a member's Discord roles into the permission tiers they hold.

    Members are read with getattr rather than against a typed protocol, so an object that is not
    a guild member at all resolves to no permissions instead of raising.

    Roles first, always, and with no I/O. GitHub is a second route only where a server has turned
    it on, which is the only time this is handed `grants` (issue #94); without it the gate answers
    exactly as it did before there was a route.
    """

    def __init__(self, settings: RoleNames, grants: GrantsTiers | None = None) -> None:
        self._settings = settings
        self._grants = grants

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

    def github_route(self, allowed: Collection[CommandRole]) -> GitHubRoute | None:
        """What GitHub would have to vouch for to let a member in whom `allows` turned away.

        Issue #94. The lowest level that fits among the tiers the command is open to, or None
        where GitHub cannot stand in at all: the server never turned the route on, or nothing the
        command allows is a tier GitHub stands in for.

        Only tiers whose role setting still names a role count. Emptying a role setting is how a
        server turns a tier off, and the route must not turn it back on. That same condition is
        what makes `denial` name at least one role whenever a route exists, so the sentence the
        route adds always has a role to follow rather than "You are not allowed".

        Synchronous and free, so a caller can ask it before deciding whether to defer.
        """
        if self._grants is None:
            return None
        level = next(
            (
                level
                for tier, level in _STANDS_IN
                if tier in allowed and self._settings.role_names(tier)
            ),
            None,
        )
        if level is None:
            return None
        return GitHubRoute(level, self._grants)


class MemberTiers:
    """Whether a member holds a tier in a server now, asked of Discord rather than remembered.

    Found reviewing #201. A slash command is gated on the roles Discord sends with it, but a board
    link is followed up to ten minutes later, from a browser, with no interaction to read roles
    off. So this fetches the member and puts them through the same gate the command used, which is
    what makes the second answer comparable with the first.

    The same gate includes its GitHub route where a server turned one on (issue #94), so a member
    GitHub let run `/board link` is asked about on GitHub again when the link is followed, rather
    than refused for lacking a role the command never asked them for.
    """

    def __init__(self, members: FindsMembers, gate: PermissionGate) -> None:
        self._members = members
        self._gate = gate

    async def holds(
        self, *, guild_id: int, discord_user_id: int, tiers: Collection[CommandRole]
    ) -> bool:
        """Any of `tiers`, as the command's gate reads them: an administrator holds every one.

        Somebody no longer in the server holds none, and is not asked about on GitHub: leaving
        the server ends what the server let them do. Where Discord cannot be asked this raises
        `DiscordGatewayError`, and where GitHub stood in and cannot be asked it raises
        `GitHubError`. Each is a different answer from no, and the caller's to refuse on.
        """
        member = await self._members.member(guild_id=guild_id, user_id=discord_user_id)
        if member is None:
            return False
        if self._gate.allows(member, tiers):
            return True
        route = self._gate.github_route(tiers)
        if route is None:
            return False
        return await route.refusal_for(guild_id=guild_id, discord_user_id=discord_user_id) is None

    def github_could_stand_in(self, tiers: Collection[CommandRole]) -> bool:
        """Whether GitHub access could let anybody hold any of `tiers` on this deployment at all.

        Issue #94. Only so a refusal can say what would have let them in. On a deployment that
        never turned the route on, or for tiers GitHub stands in for none of, a sentence naming
        GitHub access sends a member looking for a way in that does not exist there. Asks nobody.
        """
        return self._gate.github_route(tiers) is not None
