from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shannon.domain.enums import CommandRole


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHANNON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Credentials are SecretStr so that printing, logging or serialising this object shows
    # asterisks. A plain str would put the bot token in the logs on the first careless
    # logger.debug of the settings.
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon"
    )
    discord_token: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")

    role_admin: str = "Admin"
    role_project_manager: str = "Project Manager"
    role_reviewer: str = "Reviewer"
    role_developer: str = "Developer"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = Field(default=10.0, gt=0)

    # The webhook endpoint only writes a delivery down; these govern the worker that then acts
    # on it. The defaults ride out roughly two hours of Discord being unreachable.
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_batch_size: int = Field(default=10, gt=0)
    # Sixteen is what the two hours in WorkerSettings actually costs. This is the number that
    # ships, so it is the one the documented window has to be computed from.
    worker_max_attempts: int = Field(default=16, gt=0)
    worker_max_backoff_seconds: float = Field(default=900.0, gt=0)
    # How long a leased delivery stays claimed. A worker killed mid-delivery leaves its rows
    # untouched until this passes, so this is also how long that work waits. It has to cover a
    # whole batch at its worst, which is batch_size deliveries each taking the full timeout.
    worker_lease_seconds: float = Field(default=900.0, gt=0)
    worker_delivery_timeout_seconds: float = Field(default=60.0, gt=0)
    # How long shutdown waits for the delivery in hand to finish. Longer than one delivery
    # normally takes, and well inside the ten seconds a container gets before SIGKILL.
    worker_shutdown_grace_seconds: float = Field(default=5.0, ge=0)
    # Payloads hold issue titles, comment bodies and author names, so finished deliveries do not
    # sit around indefinitely.
    delivery_retention_days: int = Field(default=7, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _lease_covers_a_whole_batch(self) -> Settings:
        """Refuse a lease shorter than the batch it has to cover.

        A batch is leased all at once and worked one delivery at a time. If the lease lapses
        mid-batch, another replica claims rows still in flight and the comment goes out twice.
        """
        needed = self.worker_batch_size * self.worker_delivery_timeout_seconds
        if self.worker_lease_seconds < needed:
            raise ValueError(
                f"worker_lease_seconds ({self.worker_lease_seconds}) must be at least "
                f"worker_batch_size x worker_delivery_timeout_seconds ({needed}), or a batch "
                "can outlive its own lease"
            )
        return self

    def role_display_names(self, role: CommandRole) -> tuple[str, ...]:
        """Configured Discord role names for a permission tier, as typed."""
        raw = {
            CommandRole.ADMIN: self.role_admin,
            CommandRole.PROJECT_MANAGER: self.role_project_manager,
            CommandRole.REVIEWER: self.role_reviewer,
            CommandRole.DEVELOPER: self.role_developer,
        }[role]
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def role_names(self, role: CommandRole) -> frozenset[str]:
        """The same names lowercased, for matching against a member's roles."""
        return frozenset(name.lower() for name in self.role_display_names(role))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
