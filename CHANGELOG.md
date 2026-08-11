# Changelog

All notable changes to Shannon Bot. Entries are grouped by the GitHub issue they close.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## MVP 1 — pull request sync

Issues #2 to #26.

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

- **Local development guide** (closes #24). Setup, every setting, the Discord application, the
  GitHub webhook, the commands, which events are handled, how to run the tests, running in
  Docker, and troubleshooting. This changelog is the documentation; there is no `docs/`
  directory.
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

- **MVP 1 acceptance** (closes #26). Every item on the issue #26 checklist walked, along with
  the deliberate departures from the issue text and the known limits.
- 301 tests pass with a database, 201 pass and 100 skip without one, so a fresh clone gets a
  green run with no Docker.
- The migration was applied to an empty database, rolled back to base, and applied again. The
  applied schema was diffed back against the ORM metadata and matched exactly.
- `ruff check` and `ruff format --check` clean.

### Continuous integration

- **`uv.lock`**, so CI installs the same versions every run instead of whatever resolved that
  day. Installs use `uv sync --locked`, which fails on a lock that no longer matches
  `pyproject.toml`.
- **`.github/workflows/ci.yml`**, four jobs in parallel behind one required check:
  - Lint and format, with findings reported as inline annotations on the diff.
  - Tests on Python 3.12, 3.13 and 3.14 against a PostgreSQL service container, plus a second
    run with no database to prove the integration tier skips rather than fails on a fresh
    clone.
  - Image build with a layer cache, then a real smoke test: apply migrations from the image,
    start it, wait for the health check, post a correctly signed webhook and expect 200, post
    an unsigned one and expect 401.
  - Dependency audit against known vulnerabilities, and a secret scan over the full history.
  - Pull request runs cancel their own superseded runs; runs on main do not, because they gate
    releases.
- **`.github/workflows/release.yml`** publishes the image to GHCR with signed build provenance
  and an SBOM. Pushes to main publish `edge` for amd64; version tags publish semver tags for
  amd64 and arm64, since arm64 goes through emulation and is only worth paying for on a real
  release.
- **`.github/dependabot.yml`** for Python, actions and base image updates, with routine bumps
  grouped into one pull request.
- **Migration tests** (`tests/integration/test_migrations.py`) covering the failure this
  project is most exposed to: an ORM change landing without a matching revision. Applies
  migrations to a throwaway database, diffs the result against the models, rolls back, and
  reapplies. Also asserts there is exactly one head, since two make `upgrade head` ambiguous.

### Fixed

- `migrations/env.py` called `fileConfig` without `disable_existing_loggers=False`, so running
  Alembic in the same process as the application silenced every application logger. Only
  visible once something ran migrations in-process, which the new migration tests do.
- `alembic.ini` used the deprecated `version_path_separator` key, now `path_separator`.

---

## MVP 2 — GitHub issue sync

Issues #27 to #53. No migration: `tracked_items.github_object_type`,
`channel_mappings.object_type` and `tracked_items.priority` already carried `ISSUE` and priority
values, so MVP 1's schema absorbed MVP 2 unchanged. That is the payback for choosing `VARCHAR`
plus `CHECK` over native PostgreSQL enums.

### Changed

- **One sync service for both kinds of item** (closes #32, #52)
  - `ItemSyncService` holds the orchestration that is the same for pull requests and issues:
    find the repository, find the channel, create or update the tracked item, store the people,
    resolve mentions, write the thread, ping whoever is owed a ping.
  - A `SyncPolicy` holds the handful of decisions that differ. `PullRequestPolicy` and
    `IssuePolicy` differ in seven places: which roles are stored, how status moves, where
    priority comes from, whether the thread locks, who gets pinged, how the metadata is
    rendered, and the object type. MVP 4's project tickets become a third policy rather than a
    third copy of the orchestration.
  - All 351 MVP 1 tests passed against the generalised service without being changed, which is
    the evidence the refactor was faithful.
  - The two webhook handler modules were near-identical and collapsed into
    `build_item_handler(service, parse)`; `pr_sync.py` and `issue_sync.py` were deleted.
  - `ReviewerNotifier` became `ActorNotifier` with the role and the wording injected. Pinging a
    reviewer and pinging an assignee were the same mechanism with different words.
  - `pull_request_thread_name` and `issue_thread_name` had identical bodies and are now one
    `thread_name`.

### Added

- **Issue priority from labels** (closes #37). `priority: high`, `HIGH_PRIORITY`, a bare
  `HIGH`, and synonyms like `urgent` and `minor` all resolve. Several priority labels at once
  resolve to the highest, so a mislabelled item is escalated rather than buried. No priority
  label means `UNSET`.
- **Issue URL parser** (closes #28). The mirror of the pull request parser, sharing its path
  splitting. A pull request link pasted into `/issue` is rejected by name, and the reverse
  still holds.
- **Issue fetching** (closes #30). `get_issue` on the GitHub client. It rejects a number that
  turns out to be a pull request: GitHub serves pull requests from the issues endpoint, marked
  only by a `pull_request` key, and without the check `/issue` pointed at a pull request would
  track it a second time under the wrong type.
- **Issue webhook parsing** (closes #31, #27). `issues` events are routed and parsed into the
  same snapshot shape the REST client produces. Six actions are handled: opened, edited,
  closed, reopened, labeled, assigned.
- **Issue metadata formatter** (closes #36). The same block as pull requests minus the
  reviewers line, because GitHub issues have no reviewers and an always-empty field is noise.
- **Issue threads** (closes #33, #34, #35, #38). Issues are stored in `tracked_items` under
  `github_object_type = ISSUE`, so an issue and a pull request sharing a number stay separate
  rows. Author and assignees are stored; reviewers never are. Threads are created in the
  issue channel, updated in place, and renamed only when the title actually changed.
- **Assignee pinging** (closes #39). Each assignee is pinged once, on the same
  `notified_at` mechanism as reviewers. Reassignment pings the new person and not the old one.
- **Close and reopen** (closes #40). Closing sets status to `DONE` and locks the thread;
  reopening unlocks it and sets status back to `NOT_REVIEWED`. Reopening only resets a status of
  `DONE` rather than forcing `NOT_REVIEWED` on every sync, so MVP 3's status commands will not
  be overwritten by the next webhook.
- **Comment mirroring** (closes #41). `issue_comment.created` is posted into the thread of
  whatever it was left on, with the commenter, a quoted and truncated body, the timestamp and
  the link. Comments on pull requests are mirrored too, not only issues.
- **Review mirroring** (no issue; `requirements.md` lists the event). A submitted
  `pull_request_review` is posted into the pull request's thread, saying whether it approved,
  requested changes or left a review, with the body quoted the same way a comment's is. An
  approval with no body still posts, because the verdict is the point. Reviews do not move the
  workflow status; approving is not `/SET_READY_FOR_MERGE`, which arrives in MVP 3.
  - GitHub reports the verdict lowercased on webhooks and uppercased on the REST API, so it is
    normalised on the way in and nothing downstream has to know.
  - Finding the thread is identical work for a comment and a review, so `ItemNoteMirror` does
    it once and only the rendering is injected. `CommentMirror` was folded into it.
- **`/issue <issue_link>`** (closes #29, #42). Same permissions as `/pr`: developer, reviewer or
  project manager. The manual sync service is now shared, parameterised by the link parser and
  the fetch, so `/pr` and `/issue` cannot drift apart.
- **`/set_channel <type> <channel>`** (no issue). Issues need a channel of their own and
  `/register` only ever mapped pull requests. Needs admin or project manager. Only text and
  forum channels are accepted, because nothing else can hold a thread.
- **Issues fall back to the pull request channel** when nothing has been mapped for them, so a
  guild that ran `/register` and nothing else still sees its issues. `/set_channel` moves them
  out when a server wants them separate. The fallback lives in
  `ChannelMappingStore.resolve`, kept distinct from `get`, which still answers only what was
  actually configured, because `/set_channel` needs the literal answer.
- **Assignee, label and reviewer removals are mirrored** (`unassigned`, `unlabeled`,
  `review_request_removed`), alongside the additions they undo. Handling only one half left a
  thread claiming someone was still assigned until some later event happened to correct it.

### Security

- **Mass mentions are suppressed** on everything the bot sends. GitHub comment bodies are
  mirrored into Discord, so a comment containing `@everyone` would otherwise ping the whole
  server. Only the user mentions the bot builds itself resolve.

### Fixed

- **Locking no longer archives the thread.** Archiving hides a thread and makes every later
  edit fail, and a closed issue still receives label and assignment events that have to reach
  its metadata. Caught by a test asserting a second close still updates.
- **A removed reviewer is no longer put straight back.** `review_request_removed` names the
  person removed in the same `requested_reviewer` field a request uses, and the fold that makes
  `review_requested` work would have undone the removal. The fold now only runs for
  `review_requested`.

### Tests

- 520 passing, up from 351. Without a database, 355 pass and 165 skip, so a fresh clone still
  goes green with no Docker.
- Issue URL parsing (#43), webhook parsing (#44) against a recorded GitHub payload, sync
  service (#45), webhook to thread creation (#46) and update (#47), close and reopen (#48),
  comment mirroring (#49), and the `/issue` command (#50).
- Comment matching is covered for pull requests specifically, because that is the case the
  number lookup exists for.

### Departures from the issue text

Three, each deliberate.

- **`/set_channel` exists at all.** No issue asks for it. Issues need somewhere to post and
  `/register` only ever mapped pull requests, so without it the requirements' own table of
  per-type channels could not be filled in.
- **More webhook actions than the issues list.** Issue #27 names six issue actions and issue #9
  named seven pull request actions, none of them removals. Handling only the additions left a
  thread claiming someone was still assigned, or still carrying a label that had been taken
  off, until some later event happened to correct it. `unassigned`, `unlabeled` and
  `review_request_removed` are handled for that reason.
- **Comments are mirrored for pull requests as well as issues.** Issue #41 speaks only of
  tracked issues, but GitHub sends one event type for both and the lookup is identical, so
  restricting it would have meant deliberately dropping pull request comments.

### Known limits

- **Issues land in the pull request channel until `/set_channel issue` is run.** That is the
  fallback working rather than a failure, but a server that wants them separate has to say so.
- **Review edits and dismissals are not mirrored**, matching how comment edits are treated: the
  thread records what was said when it was said.
- **Inline review comments are not mirrored.** `pull_request_review_comment` is a separate
  event and appears nowhere in the requirements. A review that only carries inline notes posts
  as "left a review" with no body.
- **Comment edits and deletions are not mirrored**, so a comment in Discord records what was
  said when it was said.

### Verified

- **MVP 2 acceptance** (closes #53). `/issue` works, issue webhooks create and update threads,
  duplicate deliveries do not duplicate anything, assignee pinging works where links exist,
  priority is read from labels, closing locks and reopening unlocks, comments sync, PostgreSQL
  state is correct, and MVP 1 pull request sync still works.
- `alembic upgrade head` is a no-op and `alembic heads` still reports `0001`, confirming MVP 2
  added no revision. The migration-versus-models test still passes.
- `ruff check` and `ruff format --check` clean.
- No MVP 3 or MVP 4 functionality: no status or priority commands, no GitHub Projects sync.
