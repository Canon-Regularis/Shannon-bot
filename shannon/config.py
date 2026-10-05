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

    # The second registered application, and the only one that asks GitHub for a scope. Issue #170.
    #
    # A project board cannot be reached through the App at all: GitHub publishes no App permission
    # for a USER-owned Projects v2 board, and granting an installed App an organisation permission
    # suspends its event delivery until an admin accepts, which would stop every webhook in every
    # registered repository. OAuth scopes have neither problem, and `project` covers user and
    # organisation projects alike - so boards go through a classic OAuth App, registered separately,
    # which touches nothing about the App and needs no reinstall.
    #
    # One per person rather than one per deployment. Whoever links a board authorises this
    # themselves, that authorisation is what the board is read under, and a card moved from Discord
    # is moved as whoever moved it. What this replaced was a single token belonging to one human
    # account, shared by every server, under which every write appeared to be theirs.
    github_board_client_id: str = ""
    github_board_client_secret: SecretStr = SecretStr("")

    # The key the authorisations above are encrypted with, and the only encryption in this schema.
    #
    # Everything else this bot stores about somebody is a fact ABOUT them. A board authorisation is
    # a thing that ACTS as them, so it is encrypted at rest with a key that lives here and never in
    # the database - which is what makes a stolen copy of the table worth nothing on its own.
    #
    # A comma-separated list, newest first. The cipher encrypts with the first key and decrypts with
    # any of them, so rotating is "put the new key in front, deploy, drop the old one next deploy"
    # rather than every person who linked a board doing it again.
    #
    # Unset or unreadable means boards are off and said so, loudly, once. Deliberately not a
    # validator that refuses to start: a lock added over a working system must not become the reason
    # the system stops, and taking webhooks and deliveries down over a board setting would be a
    # worse failure than the one this guards against.
    board_credential_key: SecretStr = SecretStr("")

    # The bot's own Discord application, as an OAuth client: every one-time link now goes through
    # it before GitHub, so that Discord can say which member is holding the browser. Found reviewing
    # #201 - a link that went straight to GitHub was finished by whoever clicked it, so a forwarded
    # one signed its issuer in as somebody else. Asked for `identify` and nothing more.
    #
    # The same application the bot token belongs to, so nothing new is registered: its OAuth2 page
    # in the Developer Portal has the client id, a secret to reset, and the redirect to add -
    # `<SHANNON_PUBLIC_BASE_URL>/oauth/discord/callback`.
    #
    # Unset means every link refuses, `/link`, `/register`, `/unregister` and `/board` alike, rather
    # than handing out one that skips the check. That is failing closed, and it was chosen.
    discord_client_id: str = ""
    discord_client_secret: SecretStr = SecretStr("")

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
    # The origin every OAuth `redirect_uri` is built from, Discord's and GitHub's. Empty makes every
    # command that hands out a link refuse rather than hand out one that goes nowhere.
    public_base_url: str = ""
    # Whether a link nobody proved may be used to write to GitHub. `/link` records a login an
    # admin typed and nobody checked, so a wrong one acts on a real repository under somebody
    # else's name. Off by default, because turning it on before people have run `/link` refuses
    # every assignment in the server; until then an unproved link still works and the reply says
    # so. Ignored where the App's client id or secret, or the public URL, is unset, since no link
    # could be proved there at all. Deliberately NOT ignored where only the Discord sign-in is
    # missing: `/link` refuses there naming the settings it lacks, and leaving Discord unset must
    # not be a way to switch enforcement off. See `services/access.py`.
    require_proved_links: bool = False
    github_timeout_seconds: float = Field(default=10.0, gt=0)

    # Whether THIS process reads project boards at all. Polled rather than delivered: GitHub
    # sends projects_v2 webhooks for organisation projects only, and a personal account gets no
    # such event at all.
    #
    # Run the poller in ONE replica. Nothing elects a leader, so two pollers racing on one card
    # can each put its row back and undo the other's finished move, permanently. This is the
    # switch that says which replica - a job the number below used to do, badly and now not at
    # all: a board is linked by /board link, so a second replica would start polling the moment
    # somebody ran the command, with no environment change anywhere to notice.
    poll_boards: bool = True

    # A board named in the environment rather than linked, which NOTHING READS ANY MORE. Since
    # issue #170 a board is read under the authorisation of whoever linked it, and a board named
    # here has nobody recorded against it, so it cannot be opened; the container says so at
    # boot. Kept so an existing .env still starts, and slated for removal. Run /board link.
    github_project_number: int = Field(default=0, ge=0)
    # Who owns that board, read no more than the number above is. `/board link`'s owner option
    # is where an owner is named now.
    github_project_owner: str = ""
    # How often a linked board is read. Two seconds, matching the delivery worker, and that
    # parity is the point: a ticket and an issue now reach Discord on the same clock, where a
    # ticket used to wait a mean of thirty seconds for a sixty-second one. Issue #189.
    #
    # Affordable because of exactly one fact, measured rather than assumed: GitHub honours
    # `If-None-Match` on the project items endpoint, and a 304 carries no body AND spends no
    # rate-limit budget. A board nobody touched therefore costs one request and nothing else, so
    # thirty times the passes cost no more units per hour than sixty-second polling did.
    #
    # Floored at one second rather than merely positive. Below that the primary budget is still
    # untouched - 304s are free - but the request RATE starts to matter: GitHub's secondary limit
    # is about how fast requests arrive, and tripping it lengthens with every request made during
    # one. The floor is the one part of this nobody should be able to tune into a ban.
    project_poll_seconds: float = Field(default=2.0, ge=1.0)
    # Whether dragging a card may change the item's status. Off: nothing GitHub sends with a
    # board says who moved a card, so the poller cannot ask the permission question the slash
    # commands ask. A draft card is unaffected either way - its status is its column.
    board_may_set_status: bool = False
    # Whether a status set HERE may drag the card on the board, which is the mirror of the
    # line above. ON: a server that has run `/board link` has asked for its board to be the
    # truth, and a `/status` that writes the label and leaves the card where it was does half
    # the job silently (issue #179).
    #
    # The card is written as whoever moved it, with the `project` scope they granted through
    # `/board authorise` or `/board link`, which the sign-in checks GitHub actually granted.
    # Somebody who has granted nothing is refused by `/status` and `/priority` before anything
    # is written. A write GitHub refuses anyway is logged and swallowed: the label, the row and
    # the thread have all landed by then, so reporting a failure would report one that did not
    # happen.
    #
    # Turning this OFF stops the card being written and nothing else. It used to withhold the
    # whole board reader, which also took away the rule that refuses a move the board's own
    # column order forbids - a READ, costing nothing but a read, that has no business behind a
    # write flag. That was the other half of #179.
    board_may_move_cards: bool = True

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
