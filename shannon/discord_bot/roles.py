"""The Discord role names a server has given each permission tier.

Apart from `config` because splitting a comma-separated string and lowercasing it for matching
is the permission system's business, not the environment's. `Settings` keeps the four strings it
reads; what they mean is decided here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from shannon.config import Settings


class CommandRole(StrEnum):
    """Permission tiers a Discord member can hold."""

    ADMIN = "ADMIN"
    PROJECT_MANAGER = "PROJECT_MANAGER"
    REVIEWER = "REVIEWER"
    DEVELOPER = "DEVELOPER"


@dataclass(frozen=True, slots=True)
class ConfiguredRoles:
    """What each tier is called on this server.

    No defaults. Restating "Admin" here as well as on `Settings` is exactly the drift that the
    worker settings test exists to catch, so the strings have one home and this reads them.
    """

    admin: str
    project_manager: str
    reviewer: str
    developer: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ConfiguredRoles:
        return cls(
            admin=settings.role_admin,
            project_manager=settings.role_project_manager,
            reviewer=settings.role_reviewer,
            developer=settings.role_developer,
        )

    def role_display_names(self, role: CommandRole) -> tuple[str, ...]:
        """Configured names for a tier, as typed."""
        raw = {
            CommandRole.ADMIN: self.admin,
            CommandRole.PROJECT_MANAGER: self.project_manager,
            CommandRole.REVIEWER: self.reviewer,
            CommandRole.DEVELOPER: self.developer,
        }[role]
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def role_names(self, role: CommandRole) -> frozenset[str]:
        """The same names lowercased, for matching against a member's roles."""
        return frozenset(name.lower() for name in self.role_display_names(role))
