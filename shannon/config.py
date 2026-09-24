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

    # Credentials are SecretStr so the first careless `logger.debug` of the settings prints
    # asterisks rather than the bot token.
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://shannon:shannon@localhost:5433/shannon"
    )
    discord_token: SecretStr = SecretStr("")
    github_webhook_secret: SecretStr = SecretStr("")

    # `/register` is open to anybody with the Admin role in any server this bot joins, so a
    # personal access token would expose every private repository it can read; an installation
    # token sees one account. The client id is the JWT issuer and the OAuth client id at once.
    github_app_client_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_client_secret: SecretStr = SecretStr("")
    github_app_webhook_secret: SecretStr = SecretStr("")

    # GitHub publishes no App permission for a USER-owned Projects v2 board - the Projects
    # permission exists at organisation level only - so the board reader keeps a token of its own.
    #
    # An organisation's board is not read through the App either, although it could be. Granting
    # a new permission to an installed App suspends its event delivery until an admin accepts, so
    # taking that route would stop every webhook in every registered repository until somebody
    # clicked a button, in order to turn on a feature that ships off. One token covers both kinds
    # of board and costs nothing to anybody not using one.
    #
    # Left unset, the board reads through the ordinary client rather than not at all.
    github_project_token: SecretStr = SecretStr("")

    role_admin: str = "Admin"
    role_project_manager: str = "Project Manager"
    role_reviewer: str = "Reviewer"
    role_developer: str = "Developer"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # The commit the running image was built from, stamped in by the Dockerfile and reported by
    # `/health`. Kept out of `.env.example`: compose passes that file as real environment
    # variables, which beat the image's own, so a line there would pin this to a stale commit.
    build: str = "unknown"

    github_api_url: str = "https://api.github.com"
    # `authorize` and `access_token` live on `github.com`, not the API host.
    github_oauth_url: str = "https://github.com"
    # The origin the OAuth `redirect_uri` is built from. Empty makes `/unregister` refuse rather
    # than hand out a link that goes nowhere.
    public_base_url: str = ""
    # Whether a link nobody proved may be used to write to GitHub. `/link` records a login an
    # admin typed and nobody checked, so a wrong one acts on a real repository under somebody
    # else's name. Off by default, because turning it on before people have run `/link` refuses
    # every assignment in the server; until then an unproved link still works and the reply says
    # so. Ignored where the round trip is not configured at all, since refusing a command nobody
    # could satisfy is only a way to break it.
    require_proved_links: bool = False
    github_timeout_seconds: float = Field(default=10.0, gt=0)

    # Whether THIS process reads project boards at all. Polled rather than delivered: GitHub
    # sends projects_v2 webhooks for organisation projects only, and a personal account gets no
    # such event at all.
    #
    # Run the poller in ONE replica. Nothing elects a leader, so two pollers racing on one card
    # can each put its row back and undo the other's finished move, permanently. This is the
    # switch that says which replica - a job the number below used to do, badly and now not at
    # all: a board is linked by /set_board, so a second replica would start polling the moment
    # somebody ran the command, with no environment change anywhere to notice.
    poll_boards: bool = True

    # A DEFAULT board, for a deployment that has not run /set_board yet. Not "the" board any
    # more: a board belongs to a repository and is recorded on its row, which is what lets two
    # servers each mirror their own. Zero means none.
    #
    # These two stop applying anywhere the moment ANY repository carries a board of its own.
    # Half-honouring them would poll one server out of the database and another out of the
    # environment with nothing saying which was which.
    github_project_number: int = Field(default=0, ge=0)
    # Who owns that default board, where it is not the registered repository's own owner. Empty
    # means it is. Worth a setting rather than an assumption because the number above is a
    # sequence GitHub keeps per account: the wrong owner does not reliably answer 404, it can
    # answer with a real board belonging to somebody else, and its cards would be mirrored in
    # here as if they were this repository's work.
    github_project_owner: str = ""
    project_poll_seconds: float = Field(default=60.0, gt=0)
    # Whether dragging a card may change the item's status. Off: nothing GitHub sends with a
    # board says who moved a card, so the poller cannot ask the permission question the slash
    # commands ask. A draft card is unaffected either way - its status is its column.
    board_may_set_status: bool = False

    # The webhook endpoint only writes a delivery down; these govern the worker that acts on it.
    # The defaults ride out roughly two hours of Discord being unreachable.
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_batch_size: int = Field(default=10, gt=0)
    # Sixteen attempts is what the two-hour window above costs.
    worker_max_attempts: int = Field(default=16, gt=0)
    worker_max_backoff_seconds: float = Field(default=900.0, gt=0)
    # How long a leased delivery stays claimed. A worker killed mid-delivery leaves its rows
    # untouched until this passes, so this is also how long that work waits.
    worker_lease_seconds: float = Field(default=900.0, gt=0)
    worker_delivery_timeout_seconds: float = Field(default=60.0, gt=0)
    # How long shutdown waits for the delivery in hand. Well inside the ten seconds a container
    # gets before SIGKILL.
    worker_shutdown_grace_seconds: float = Field(default=5.0, ge=0)
    # Payloads hold issue titles, comment bodies and author names, so finished deliveries are
    # not kept indefinitely.
    delivery_retention_days: int = Field(default=7, gt=0)

    # Whether `/log_conversation` works, and whether the gateway asks for the message content
    # intent. Off by default: the intent is privileged, so without the box ticked in the Discord
    # Developer Portal the identify closes with 4014, discord.py raises
    # `PrivilegedIntentsRequired`, and the process halts - webhook mirror, delivery worker and
    # poller with it. Tick the box before setting this.
    capture_discord_messages: bool = False
    # How long a logged thread has to go quiet before what was said in it is published.
    conversation_quiet_seconds: float = Field(default=60.0, gt=0)
    # How often the flusher looks; most passes are one grouped query that finds nothing.
    conversation_flush_tick_seconds: float = Field(default=5.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("github_app_private_key")
    @classmethod
    def _unescape_newlines(cls, value: SecretStr) -> SecretStr:
        r"""Turn the two-character `\n` of an environment variable into real newlines.

        A PEM is multi-line and `.env` is not, so the key is written on one line with escaped
        breaks. Without this the key parses as nothing, every repository reports as one this bot
        cannot see, and the message says nothing about a key.
        """
        raw = value.get_secret_value()
        return SecretStr(raw.replace("\\n", "\n")) if "\\n" in raw else value

    @model_validator(mode="after")
    def _lease_fits_a_batch(self) -> Settings:
        """Refuse a lease shorter than the batch it has to cover.

        A batch is leased at once and worked one delivery at a time. If the lease lapses
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
