# Changelog

All notable changes to Shannon Bot. Entries are grouped by the GitHub issue they close.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Project scaffolding** (no issue, required before anything else can install)
  - `pyproject.toml` with the runtime and dev dependency sets, pytest and ruff config.
  - `docker-compose.yml` running Postgres 17 for local development and the integration test tier.
  - `.env.example` listing every setting with placeholder values.
  - `shannon/config.py`, settings read from `SHANNON_`-prefixed environment variables.
  - `shannon/domain/`, the pure layer: status and priority enums, snapshot dataclasses,
    and the error hierarchy. Nothing in here imports the rest of the project, which is what
    lets the unit tests run with no database and no network.

- **PostgreSQL schema and initial migration** (closes #2)
  - Six tables: `repositories`, `channel_mappings`, `tracked_items`, `item_assignments`,
    `webhook_events`, `user_links`.
  - Alembic revision `0001` creates all of them. Verified against a live database by diffing
    the applied schema back against the ORM metadata.
  - Duplicate PR threads are blocked by a unique constraint on
    (`repository_id`, `github_object_type`, `github_object_id`) rather than by application code.
  - One repository per guild comes from a unique `discord_guild_id`. `github_repo_id` is unique
    too, so an inbound webhook resolves to exactly one guild. The side effect is that a
    repository cannot be registered in two servers at once.
  - Enums are stored as `VARCHAR` plus a `CHECK` constraint, not native PostgreSQL enums, so
    later MVPs can add status and priority values without an `ALTER TYPE` migration.
  - `user_links` is not in the original requirements table list. It was added because reviewer
    pinging needs a GitHub login to Discord account mapping and no other table holds one.

- **GitHub webhook endpoint** (closes #3)
  - `POST /webhooks/github`, reading `X-GitHub-Event`, `X-GitHub-Delivery` and
    `X-Hub-Signature-256`.
  - `EventRouter` maps an event type to the handler that owns it, so the HTTP route knows
    nothing about pull requests.
  - Unsupported events and unsupported actions return 200 with `status: ignored`. A non-2xx
    would make GitHub mark the delivery failed and retry something the bot will never handle.
  - GitHub's one-off `ping` event is acknowledged.
  - Missing headers, non-JSON bodies and non-object bodies return 400 with a specific message.

- **Webhook signature verification** (closes #4)
  - HMAC-SHA256 over the raw request body, compared in constant time.
  - Missing, malformed and mismatched signatures all return 401, and the body is never parsed
    or dispatched.
  - An unset `SHANNON_GITHUB_WEBHOOK_SECRET` rejects every delivery rather than waving them
    through.

- **Webhook idempotency** (closes #5)
  - Each delivery ID is claimed in `webhook_events` before the handler runs. A repeat delivery
    returns `status: duplicate` and never reaches the handler, so no second thread and no second
    tracked item.
  - The claim is a single `INSERT ... ON CONFLICT DO NOTHING RETURNING`, so two deliveries
    racing on the same ID cannot both win. Verified with eight concurrent claims against a real
    database.
  - A handler that raises releases its claim, because GitHub retries failed deliveries with the
    same ID and that retry is real work rather than a duplicate.
  - Duplicates are logged.

- **Pull request URL parser** (closes #7)
  - Pulls owner, repository and number out of a GitHub pull request link.
  - Accepts what people actually paste: no scheme, `www.`, a trailing slash, `/files`,
    `#discussion_r1`, a query string, and Discord's `<...>` embed suppression.
  - Rejects an issue link by name rather than as generic junk, because pasting one into `/pr`
    is a normal mistake.
  - Rejects other hosts, other schemes, missing numbers, and owner names GitHub would not allow.

- **GitHub API client** (closes #16)
  - `GitHubClient` is a Protocol. Commands and services depend on that, so nothing outside
    `shannon/github/` knows GitHub is reached over HTTP.
  - Fetches repositories and pull requests.
  - Failures come back as typed errors rather than raw HTTP: not found, auth, rate limit, and
    unavailable. A spent rate limit arrives from GitHub as a 403 and is told apart from a real
    permission problem by the `x-ratelimit-remaining` header.
  - A pull request response embeds its own repository under `base.repo`, which saves a second
    call. The client falls back to fetching the repository when it is missing.
  - REST payloads and webhook payloads are turned into the same snapshot type by one mapping
    module, so downstream code never branches on where the data came from.

- **`/register <github_repo_link>`** (closes #6)
  - Validates the link, fetches the repository from GitHub, and binds it to the guild.
  - The channel the command was run in becomes the home for pull request threads.
  - Rejects a guild that already has a repository, and a repository already bound elsewhere.
  - A rejected registration leaves no rows behind.

- **Discord permission checks** (closes #17)
  - Role names are read from configuration, so a server can call its reviewers whatever it
    likes without a code change.
  - `/register` needs admin or project manager. `/pr` needs developer, reviewer or project
    manager.
  - A guild Administrator passes every check, which also means the bot is usable in a server
    that never set the role names up.
  - Refused commands answer with the roles that would have worked.

- **Pull request webhook payload parser** (closes #9)
  - Turns a `pull_request` body into the same snapshot the REST client produces.
  - `review_requested` carries the person just added at the top level of the event, and their
    appearance in `requested_reviewers` is not guaranteed. That reviewer is folded in.
  - Out-of-scope actions and unusable bodies return nothing, which callers read as "no work"
    rather than as a failure. GitHub sends plenty of events this bot has no opinion about.

- **Discord metadata formatter** (closes #12)
  - Renders the metadata block that lives at the top of a thread.
  - Linked accounts render as Discord mentions, unlinked ones as plain GitHub usernames.
  - Empty authors, assignees, reviewers and tags read as `None` rather than as blanks.
  - Timestamps use Discord's own markup so each reader sees their own timezone.
  - Output is truncated to fit Discord's 2000 character limit.

- **Discord thread creation** (closes #11)
  - `ThreadGateway` is a Protocol, which is what lets the sync path be tested without a gateway
    connection.
  - Text channels and forum channels are both supported, because a server may keep pull
    requests in either.
  - Thread IDs and metadata message IDs are stored on the tracked item.

- **Discord thread update** (closes #13)
  - Edits the existing metadata message rather than posting a new one.
  - Renames the thread only when the title actually changed, because Discord rate limits
    renames hard.
  - A metadata message someone deleted is replaced and the new ID adopted.
  - No update path can create a second thread.

- **Pull request sync service** (closes #10)
  - One service, called by both the webhook pipeline and `/pr`, so a manually synced pull
    request and an automatically synced one cannot drift apart.
  - New pull requests get one tracked item at status `NOT_REVIEWED` and priority `UNSET`.
  - Existing ones are updated in place and keep their thread.
  - Author, assignees and reviewers are stored, and reassignment removes whoever is no longer
    on the pull request.
  - GitHub logins are stored lowercased. They are case insensitive on GitHub's side, and
    without normalising them the unique constraint would accept both `Octocat` and `octocat`.
  - Discord is called outside the database transaction. Holding a transaction open across a
    network call would let a slow gateway block the database, and a rollback would throw away
    work Discord had already done.

- **`/pr <pr_link>`** (closes #8)
  - Syncs a pull request by hand instead of waiting for a webhook.
  - Rejects links to repositories other than the registered one, and issue links, before
    calling GitHub at all.
  - Reports GitHub and Discord failures as messages rather than raising at the user.

- **Reviewer pinging** (closes #14)
  - Each reviewer is pinged once. `item_assignments.notified_at` is the record of who has
    already been told.
  - A reviewer who is removed and requested again gets pinged again, which is what re-requesting
    a review is asking for.
  - The stamp is written only after the message is out, so a Discord failure leaves the ping
    owed rather than silently swallowed.
  - Unlinked reviewers are still named in plain text, so the thread records who GitHub asked
    for even when nobody has run `/link` for them.
  - **`/link <github_username> [member]`** was added to populate the mapping. Not in the
    original requirements; reviewer pinging needs somewhere to read a Discord ID from. Anyone
    can link themselves, and speaking for someone else needs admin or project manager.

- **Closed and reopened pull requests** (closes #15)
  - A `State:` line in the metadata reading Open, Closed or Merged. GitHub reports only open or
    closed and carries merging as a separate flag, so the three-way answer is derived in one
    place.
  - Closing updates the existing thread and leaves it unlocked. Locking and `/SET_DONE` are
    MVP 3.
  - Closing does not touch the workflow status.

### Tests

- **Webhook parsing** (closes #18). All seven supported actions, plus a verbatim GitHub payload
  recorded to `tests/fixtures/payloads/` so the parser is checked against the real shape and not
  only against the test builder. Missing people, missing labels, nulls where lists belong,
  unparseable timestamps and deleted author accounts are all covered.
- **Pull request sync** (closes #19). Creation, update, repeated delivery, assignment churn,
  reviewer addition, label changes and closing. These run against a real PostgreSQL rather than
  fakes, because "a duplicate webhook does not duplicate state" is a claim about the database
  and a fake would only be testing the fake.
- **Webhook to thread creation** (closes #20) and **webhook to thread update** (closes #21).
  Full stack through the real HTTP endpoint: real signature check, real delivery guard, real
  router, real sync service, real database, with only Discord and GitHub faked.
- **`/register`** (closes #22) and **`/pr`** (closes #23). Command callbacks driven with a fake
  interaction, covering the permission gate, every error path, and the deferral that keeps
  Discord from dropping a slow interaction.

### Documentation

- **Local development guide** (closes #24). `docs/local-development.md` covers setup, every
  setting, the Discord application, the GitHub webhook, the commands, which events are handled,
  how to run the tests, running in Docker, and troubleshooting.
- The runnable entrypoint landed here too, since documenting how to run something requires it
  to be runnable: `shannon/container.py` wires the application in one place and
  `shannon/main.py` serves the webhook endpoint and the bot from a single process.
- **Container image and compose stack** (no issue, requested directly).
  - Two stage `Dockerfile`. The package is installed into a virtualenv and only that virtualenv
    is carried into the runtime layer, so pip's cache and the build inputs are not shipped.
    Runs as a non-root user.
  - The health check is a plain TCP connect, because the service deliberately exposes no health
    route and adding one would be MVP 1 scope it does not have.
  - `docker compose up -d --build` brings up PostgreSQL, applies the migrations and starts the
    bot. `migrate` runs to completion before `app` starts, so the schema is never applied by two
    replicas racing each other at boot.
  - Compose overrides the database URL to `db:5432`. The 5433 host mapping exists only for
    reaching PostgreSQL from outside the compose network.
  - `.dockerignore` keeps the build context to what the image needs, and keeps `.env` out of it.

### Security

- **Credentials are `SecretStr`** rather than plain strings. Printing, logging or serialising the
  settings object now shows asterisks. Before this, `repr(settings)` returned the Discord bot
  token, the GitHub token, the webhook secret and the database password in full, so a single
  careless `logger.debug` of the configuration, or any traceback that happened to render the
  object, would have put all four into the logs. Covered by `tests/unit/test_config_secrecy.py`,
  which asserts the masking through `repr`, `str`, `model_dump`, JSON and an actual log call.
- **Removed the `test_database_url` setting.** It was never read by the application, the test
  harness takes `SHANNON_TEST_DATABASE_URL` straight from the environment, and it put a database
  password into every settings repr.
- **Database credentials in `docker-compose.yml` are variables** with throwaway local defaults,
  so a real password can be set in `.env` without editing a tracked file.
- **`.gitignore` covers every `.env` variant**, not just `.env` and `.env.local`. A bare `.env`
  rule would have missed `.env.production`. Key material (`*.pem`, `*.key`, `*.p12`, `*.pfx`,
  `secrets/`) is ignored too, in both git and the Docker build context. Anything copied into an
  image stays in that layer even if a later step deletes it.
- Audited: no credential-shaped string appears in any tracked file or anywhere in the git
  history, and `.env.example` holds placeholders only, which a test now enforces.

### Changed

- **MVP 1 cleanup** (closes #25)
  - Removed six package `__init__.py` re-export facades. Every module imported from the
    concrete module, so they were indirection with no consumers.
  - Removed `create_all` and `drop_all` from `db/session.py`, `RepositoryStore.get_by_full_name`,
    `ItemAssignmentStore.link_discord_user`, and `PermissionDeniedError`. None were called.
  - Removed the `GuildMember`, `NamedRole` and `Permissions` protocols from `permissions.py`.
    The gate reads members with `getattr` so that an object that is not a guild member resolves
    to no permissions rather than raising, which left those protocols as decoration.
  - Fixed a `ReviewerNotifier` annotation in `pr_sync.py` that referenced a name the module
    never imported. Postponed annotation evaluation was hiding it.
  - Pulled the repeated integration test setup into `tests/integration/conftest.py` and
    `tests/support/db.py`.
  - Swapped the integration tier off `NullPool`, which was paying a fresh connection handshake
    for every session a service opened. The suite went from roughly 95 seconds to 59.

### Verified

- **MVP 1 acceptance** (closes #26). Every item on the issue #26 checklist walked and recorded
  in `docs/mvp-1-acceptance.md`, along with the deliberate departures from the issue text and
  the known limits.
- 301 tests pass with a database, 201 pass and 100 skip without one, so a fresh clone gets a
  green run with no Docker.
- The migration was applied to an empty database, rolled back to base, and applied again. The
  applied schema was diffed back against the ORM metadata and matched exactly.
- `ruff check` and `ruff format --check` clean.
