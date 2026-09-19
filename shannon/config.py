from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    github_webhook_secret: SecretStr = SecretStr("")

    # The GitHub App, which replaced the single personal access token this used to hold. That
    # token had to see every repository, and `/register` is open to anybody holding the Admin role
    # in any server this bot was invited to, so widening it to private repositories would have let
    # any of them mirror any private code it could read. An installation token sees one account.
    #
    # The client id is the JWT issuer and the OAuth client id at once: GitHub accepts it for both
    # and recommends it for the first, so a deployment configures one identifier instead of two
    # that have to agree.
    github_app_client_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_client_secret: SecretStr = SecretStr("")
    github_app_webhook_secret: SecretStr = SecretStr("")

    # The one credential the App cannot replace. GitHub publishes no App permission of any kind
    # for a USER-owned Projects v2 board - the Projects permission exists at organisation level
    # only - and `HttpProjectBoards` reads `/users/{owner}/projectsV2/...` because a personal
    # account is what this runs against. So the board keeps a token of its own, read by that one
    # reader and nothing else. Narrow on purpose: a leak exposes a board rather than source, and
    # it stays unset in every deployment that leaves the project number at zero.
    github_project_token: SecretStr = SecretStr("")

    role_admin: str = "Admin"
    role_project_manager: str = "Project Manager"
    role_reviewer: str = "Reviewer"
    role_developer: str = "Developer"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # The commit the running image was built from, stamped in by the Dockerfile and reported by
    # `/health`. The one setting here that nobody is meant to set, and the only one deliberately
    # left out of `.env.example`: compose hands that file to the container as real environment
    # variables, which beat the image's own, so a line there would pin the answer to whatever was
    # typed and it would go on naming that commit through every deploy afterwards.
    build: str = "unknown"

    github_api_url: str = "https://api.github.com"
    # Where `authorize` and `access_token` live, which is `github.com` rather than the API host.
    # Its own setting beside the one above so GitHub Enterprise can move both.
    github_oauth_url: str = "https://github.com"
    # The origin the OAuth `redirect_uri` is built from. Empty makes `/unregister` refuse rather
    # than hand somebody a link that goes nowhere.
    public_base_url: str = ""
    github_timeout_seconds: float = Field(default=10.0, gt=0)

    # A GitHub project board to mirror, by the number in its URL. Zero means none, which is the
    # default because a project is opt in and polling one nobody asked for would spend API calls
    # on nothing. Polled rather than delivered: GitHub sends projects_v2 webhooks for
    # organisation projects only, and a personal account gets no such event at all, so a timer
    # is the only mechanism that works for both.
    #
    # Run this in ONE replica. Nothing elects a leader, so every replica with a number set polls
    # the same board on the same interval and the two ask for the same status for the same card.
    # A move whose Discord half is refused in one replica while the other is mid-poll can end
    # with the item's row put back and the other's finished move undone, permanently: see the
    # note on two replicas in CHANGELOG.md. Set this to zero everywhere but one.
    github_project_number: int = Field(default=0, ge=0)
    project_poll_seconds: float = Field(default=60.0, gt=0)
    # Whether dragging a card may change the item's status, which is off.
    #
    # Moving an item is a permission in Discord and this is the one road around it: nothing
    # GitHub sends with a board says who moved a card, so the poller cannot ask the question the
    # slash commands ask, and anybody with access to the board could do what only a project
    # manager is allowed to do in Discord. Off, the board still mirrors its cards, opens their
    # threads and records which column each one is in; it just does not decide anything.
    #
    # A draft card is unaffected either way. Its status IS its column, it exists nowhere but the
    # board, and no Discord command can move one.
    board_may_set_status: bool = False

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

    # Whether `/log_conversation` works at all, and whether the gateway is asked for the message
    # content intent. Issue #103.
    #
    # Off by default, and the default is the point rather than caution. That intent is privileged,
    # which means a checkbox in the Discord Developer Portal: miss it and Discord closes the
    # identify with 4014, discord.py raises `PrivilegedIntentsRequired`, the bot task ends, and the
    # process halts. The webhook mirror, the delivery worker and the poller all go with it. A
    # deployment whose rollback depends on somebody having ticked a box in a web UI is a bad
    # deployment, so this is the order: tick the box, then set this. Until both are done the
    # command refuses with a sentence saying so, and everything else carries on.
    capture_discord_messages: bool = False
    # How long a logged thread has to go quiet before what was said in it is published. Long
    # enough that a conversation arrives as a conversation, short enough that nobody wonders
    # whether it worked.
    conversation_quiet_seconds: float = Field(default=60.0, gt=0)
    # How often the flusher looks. Cheap: most passes are one grouped query that finds nothing.
    conversation_flush_tick_seconds: float = Field(default=5.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("github_app_private_key")
    @classmethod
    def _unescape_newlines(cls, value: SecretStr) -> SecretStr:
        r"""Turn the two-character `\n` of an environment variable into real newlines.

        A PEM is multi-line and `.env` is not, so the key is written on one line with its breaks
        escaped. Without this the key parses as nothing, every repository reports as one this bot
        cannot see, and the message says nothing whatever about a key.

        Harmless where the value already has real newlines, since there is then nothing to
        replace, so a deployment that mounts the file instead still works.
        """
        raw = value.get_secret_value()
        return SecretStr(raw.replace("\\n", "\n")) if "\\n" in raw else value

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
