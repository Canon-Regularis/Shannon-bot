# Changelog

What has been built into Shannon Bot, and why. Each section is a stage of the plan, and most
entries name the GitHub issue they close. Between stages there are checkpoints, where the work
was going back over what already existed rather than adding anything new.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## MVP 1: pull request sync

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
  - Enums are stored as `VARCHAR` rather than native PostgreSQL enums, so later MVPs can add
    status and priority values without an `ALTER TYPE` migration. Written here at the time as
    "`VARCHAR` plus a `CHECK` constraint", which was never true of what the migration produced:
    `create_constraint` has defaulted to False since SQLAlchemy 1.4, so no CHECK is emitted and
    the column takes any string that fits. `varchar_enum` says so and the README says so; this
    line did not, and a later stage removed an enum value on the strength of it being true.
  - `user_links` is not in the original requirements table list. It was added because reviewer
    pinging needs a GitHub login to Discord account mapping and no other table holds one.

- **GitHub webhook endpoint** (closes #3)
  - `POST /webhooks/github`, reading `X-GitHub-Event`, `X-GitHub-Delivery` and
    `X-Hub-Signature-256`.
  - `EventRouter` maps an event type to the handler that owns it, so the HTTP route knows
    nothing about pull requests.
  - Unsupported events and unsupported actions return 200 with `status: ignored`. A non-2xx
    would have GitHub record a failure for something the bot was never going to act on.
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
  - A handler that raises releases its claim, so the delivery is not remembered as done. This
    was built expecting GitHub to send it again, which it does not. See the checkpoint below.
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

## MVP 2: GitHub issue sync

Issues #27 to #53. No migration: `tracked_items.github_object_type`,
`channel_mappings.object_type` and `tracked_items.priority` already carried `ISSUE` and priority
values, so MVP 1's schema absorbed MVP 2 unchanged. That is the payback for choosing `VARCHAR`
over native PostgreSQL enums.

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

---

## Checkpoint after MVP 2

Pull requests and issues both sync now, so this was a good moment to stop adding features and
go back over everything already built. Five things turned out to be broken. One of them was
bad enough to undo work in front of people.

### Fixed

- **A webhook arriving late could undo what a newer one had already done.** GitHub does not
  promise to deliver events in order, and a redelivery someone triggers by hand can turn up
  long after the event that replaced it. Acting on one of those put an old title back. Worse, a
  late `closed` shut an issue that had just been reopened, relocked its thread and set the
  status back to `DONE` in front of whoever had reopened it. Anything describing an item as it was before what we
  already have is now ignored.

  Three things deliberately do not count as late. An item with no thread yet, because skipping
  it would mean it never gets one. A missing timestamp on either side, because that proves
  nothing either way. And two events sharing a timestamp, because a burst of changes inside one
  second all carry the same one and all of them are real.

- **A comment could have landed on the wrong item.** Comments and reviews are matched by number
  rather than id, and nothing checked whether the match was an issue or a pull request.
  Numbers are unique per repository on GitHub, so it would have taken odd data to actually go
  wrong, but the payload says which kind it is and there was no reason not to use it.

- **A bad Discord token failed quietly.** The connection died with nothing in the log while the
  webhook endpoint carried on answering normally, so everything looked healthy and nothing
  reached Discord. It now says why it stopped.

- **An `assert` was doing real work in `ManualSync`.** Assertions disappear when Python runs
  with `-O`, and this one was the only thing stopping a missing item number getting through. It
  raises properly now.

- **Events we ignore were being written to the database.** Repositories emit pushes, stars and
  forks all day, and every one of them added a row to the delivery log. There is nothing to
  protect against a repeat of something we would drop anyway, so only events that can actually
  do something get recorded.

### Changed

- **A sync now says what it did.** `sync` used to return nothing to mean three separate
  things, and the `/pr` and `/issue` commands had to guess which. An unregistered repository
  and a late delivery would have produced the same, wrong, message. They have names now.
- The database step hands back either work to do or the answer itself, so the object passed
  between the two halves no longer carries fields that mean nothing in half the cases.
- Dropped a return value nobody read and an argument that was always passed, on the assignment
  store.
- The rule about late deliveries lives on its own now, with quick tests of its own covering
  timezone offsets and missing offsets. Reaching those through the database tests would have
  been slow and roundabout.

### Second look

Another pass over the whole repository, this time over the parts the first one had not read
closely.

- **A timestamp without an offset would have rendered wrong.** `datetime.timestamp()` reads a
  naive value as local time, so a Discord timestamp would have been out by whatever offset the
  host happened to run in, and silently different between machines. GitHub sends offsets, so
  nothing was visibly broken, but the parser now settles the question at the boundary and
  everything downstream gets an aware value. `domain/time.py` holds the one rule, shared with
  the late-delivery check that needed the same thing.
- **A merged pull request said it was not closed.** Merging closes a pull request on GitHub,
  and `closed` answered on the displayed state, which reads `merged`. Nothing asked the
  question yet, so nothing was wrong in practice, but it was waiting for whoever asked first.
- Pull requests and issues now share one snapshot type for the ten fields they have in common,
  rather than declaring them twice. Same for the code that reads those fields out of a GitHub
  payload, which was two near-identical blocks that could have drifted into validating
  slightly differently.
- The message formatters were three pairs of nearly the same function: the two metadata
  blocks, the two ping messages, and comments against reviews. Each pair is now one function
  with the wording passed in, so the two kinds of thread cannot drift apart by accident.

### Third look

This pass went at the database layer and the services around it, which the first two had read
past. Both findings were crashes reachable by ordinary use.

- **`/link` crashed when someone took over a GitHub name another account already held.**
  A guild can only have one row per GitHub name and one per Discord account, and those two
  halves can sit on two different rows. Editing one of them in place collided with the other,
  and the error came straight back out of the command. Two people linking themselves and then
  one claiming the other's name was enough to trigger it. The pairing is now written fresh
  after clearing whatever held either half.
- **Several webhooks arriving at once for a brand new item crashed all but one of them.**
  GitHub fires `opened` and `labeled` together for anything opened with labels, and they carry
  different delivery ids, so the duplicate guard passes both. Both then found no tracked item
  and both tried to insert one. A burst of six left five failing on the unique constraint,
  which in production means a run of 500s and those events gone for good. The insert carries
  its own conflict handling now, the same way the delivery log already did.
- **Two people running `/register` at the same moment** hit the same shape of problem, and the
  loser now hears the same thing it would have heard a second later rather than an unhandled
  error.
- Dropped `TrackedItemStore.create`, which nothing called once the race-safe version replaced
  it.

### Fourth look

Three read-throughs had stopped turning anything up, so this one changed method. Property
tests generate inputs rather than relying on the ones someone thought to write down, and
`tests/unit/test_properties.py` states what should hold for every input rather than for a
handful.

- **`/pr [` crashed the command.** An unbalanced square bracket looks like a malformed IPv6
  host, so the URL parser raised `ValueError` instead of answering. The command only expects
  its own error type, so the interaction failed with nothing useful shown. Hypothesis found it
  on its first run, from a single character. Any link the parser cannot read now comes back the
  same way.
- The four webhook parsers are now checked against arbitrary generated JSON, several hundred
  shapes each. They already held up, checked rather than assumed: these read
  untrusted input off the network, and one that raises takes the request down with it.
- Properties now cover the things that are easy to believe and hard to check by reading. Label
  order never changes the priority. Adding labels never lowers it. A snapshot is never stale
  against itself, and of two different instants exactly one is stale against the other. Every
  rendered block fits inside Discord's limit, and a thread name is never empty whatever the
  title.

### Fifth look

Reading had run dry and the generated inputs had been tried, so this one looked at what the
database is actually asked to do. Both findings were invisible at the scale anything had been
tested at, and both get worse as a repository fills up.

- **Every fetch of a tracked item quietly loaded its assignments.** The relationship was set to
  load eagerly, and nothing ever read it: assignments are always fetched through the store, one
  role at a time. It was two wasted round trips on the hottest path in the project, on every
  webhook. Reaching for the relationship now raises instead, so anyone who wants it in future
  finds out rather than paying for it silently. A repeat sync went from ten statements to
  eight.
- **Finding an item by number scanned the whole repository.** Comments and reviews look items
  up that way, because GitHub reports a pull request's issue id in those payloads. The unique
  constraint leads with the repository, so the planner was matching on that and then filtering
  every row for the number. Fine with a handful of items, linear in the repository once there
  are thousands. Migration `0002` adds the index, and the plan now matches on both columns with
  nothing left to filter.
- Both are pinned by tests that read the query log and the query plan, rather than trusting
  that they stay fixed.

MVP 2 needed no migration at all. This is the first one since the original schema, and it adds
an index rather than changing anything that is stored.

### Sixth look

The five looks before this one went after bugs in the code. This one found something else: an
assumption the code had been built on that is simply not true.

**GitHub does not redeliver failed webhooks.** Their documentation says so outright, and a
delivery counts as failed if the endpoint takes more than ten seconds. Four places in this
project said otherwise, two of them code comments justifying their logic with it.

That mattered because the handler did all of its work inside those ten seconds. A single
`issues.opened` makes up to eight Discord calls, and `thread.edit(name=...)` is rate limited to
two renames per ten minutes, where discord.py sleeps on the 429 rather than failing. So a busy
moment, a Discord hiccup, or one rename too many meant GitHub timed out, recorded a failure and
never tried again. Only a hash of the body was stored, so it could not be replayed from this
side either. The event was gone.

- **The endpoint no longer does the work.** It checks the signature, writes the delivery down
  and answers, which takes a couple of milliseconds and touches nothing that can be slow. A 200
  now means the delivery is safely recorded, not that the thread appeared. Where it did appear
  is in the worker's log and in the row's own status.
- **`webhook_events` became a queue rather than a log.** Migration `0003` adds the body, the
  attempt count, when it is next eligible, its lease and the last error. Everything is nullable
  or defaulted, so it applies to a live table with nothing to backfill.
- **A background worker does the work.** It runs beside the bot in the same process, takes a
  batch and handles it one delivery at a time in arrival order, so two events for the same item
  keep theirs. A failure waits and comes back, doubling each time up to fifteen minutes, and
  gives up after ten attempts with the reason recorded. That rides out roughly two hours of
  Discord being unreachable. Each delivery has its own timeout, so a handler that hangs on a
  rate limit cannot wedge everything behind it.
- **A worker that dies does not strand its work.** Deliveries are leased rather than simply
  marked, so anything a killed process was holding comes back once the lease runs out. Leasing
  uses `SELECT ... FOR UPDATE SKIP LOCKED`, which means a second worker takes different rows
  rather than the same ones. Only one runs today; this keeps a second one from being a problem
  later.
- **Bodies are pruned after seven days.** They hold issue titles, comment text and author names
  from private repositories, so they do not sit there indefinitely. Anything still waiting is
  left alone however old it is.

Ordering across deliveries was already handled, by the staleness guard from the first look.
That was written for a rare reordering and is now load bearing.

### Also in this pass

Three things that had been noticed before and left, plus two found on the way through.

- **Two people running `/set_channel` at once** would have both found nothing mapped and both
  inserted, and the second would have hit the unique constraint. Same shape as the two races
  already fixed, and the last one of them.
- **A message trimmed to Discord's limit could be cut mid-markdown**, leaving `**` hanging open
  and everything after it rendering as one long bold run. Trimming now stops at a line boundary,
  since each line is built balanced on its own.
- **A label with a backtick in its name broke the code span it was rendered into**, and the rest
  of the line came out as prose. GitHub allows those. The fence is now longer than any run of
  backticks inside the name, which is markdown's own answer to this.
- Every deadline in the delivery table is now set and read against the database clock. Two of
  them were being written from the application's, which would have a worker on a host whose
  clock had drifted hold a lease for longer or shorter than it believed.
- The property tests no longer fail on timing. Hypothesis fails an example that runs longer
  than its deadline, and the first call into a module pays for importing it, so a green suite
  could go red on the same code. What those tests assert is what holds, not how fast.

### Seventh look

This one went looking in the places the previous six had no reason to visit: what Discord does
on its own when nobody is watching, and what happens to an item after somebody deletes
something by hand. Seven defects, six of which end with an item going permanently quiet in
Discord while everything on the GitHub side looks fine.

- **A thread nobody talked in for a day stopped receiving updates for good.** Discord archives
  a quiet thread by itself after its auto-archive window, and then refuses every edit to it. The
  bot never set that window, so it got the one day default, and a pull request nobody discusses
  for a day is completely ordinary. The first event after the archive failed, the worker retried
  it for two hours and gave up, and so did every event after that. Threads are now opened with
  the longest window Discord offers, one week, and every write reopens the thread first if it
  needs to. Locking does the reopening in the same call rather than paying for a second one.
- **Deleting a thread killed its item.** The stored thread id was never cleared, so once someone
  tidied a channel every later event for that pull request failed on a thread that no longer
  existed, and `/pr` on it answered the same way for ever. The only way back was editing the
  database by hand. A thread that has gone is now forgotten and rebuilt, and the replacement
  carries the item's current state.
- **Opening a thread and writing in it are two calls, and the second one can fail on its own.**
  Creating a thread and posting in it need two separate Discord permissions, so a bot can be
  allowed the first and refused the second. The thread id was thrown away when the message
  failed, which left the thread orphaned and had the retry open another one beside it, ten times
  over. The id is now recorded before the failure surfaces, so the retry writes into the thread
  that already exists.
- **Two syncs could give one item two threads.** The Discord call happens outside any
  transaction, so the worker and somebody running `/pr` on the same pull request could both find
  no thread and both open one. The row kept whichever finished last and the other was stranded:
  still in the channel, never updated again, and unreachable by anything. The claim is now a
  conditional update, so exactly one wins, and the one that lost is deleted rather than left
  sitting there.
- **A comment could be dropped because it arrived a moment too early.** A comment on a pull
  request opened seconds ago can be handled while that pull request's own thread is still being
  created. That was answered as "nothing to do", which is terminal, so the comment was never
  seen again even though the thread appeared five seconds later. It now waits and is retried,
  which is what the queue is for. A comment on something genuinely untracked is still ignored,
  because that one is not coming later.
- **Missing permissions were retried for two hours.** `discord.Forbidden` is a subclass of the
  general HTTP error, so catching the general one first filed "the bot is not allowed to do
  this" as "Discord is briefly unhappy". No amount of waiting grants a permission, so these are
  now recorded and dropped on the first attempt, with the reason in the row.
- **Renaming a repository on GitHub broke `/pr` and `/issue` permanently.** Webhooks find a
  repository by its numeric id, which survives a rename, so the mirror kept working. Both
  commands compare the pasted link against the stored name, so both started answering that the
  link was for the wrong repository, with no way for an admin to correct it. Every payload
  carries the current name, so a rename is now picked up the first time anything happens in the
  repository afterwards.
- **`/register` accepted a channel that cannot hold threads.** Run inside a thread, the command
  reported that thread as its channel and stored it. `/set_channel` had checked for this since it
  was written; `/register` had not, and the failure surfaced hours later in the worker where
  there was nobody left to tell. Both commands now share one definition of what can hold a
  thread.

### Also in this pass

- The item sync service had grown a second job. Which Discord thread an item owns is now its own
  piece, `services/item_threads.py`, because the awkward parts of it, the racing, the rebuilding
  and the half-finished creation, are all about thread identity rather than about what the item
  says. The sync flow reads as one sequence again.
- Migration `0004` drops the index on `tracked_items.discord_thread_id`. Nothing has ever
  queried by thread id: every use reads the column off a row already found some other way. It
  was write cost on the busiest table in exchange for no plan anywhere, and it implied a lookup
  that no store offers. If a later command resolves an item from the thread it was run in, the
  index comes back alongside the query that needs it.
- The fake thread gateway used in tests now behaves the way the real one does about archiving
  and deletion. It had a comment saying Discord rejects writes to an archived thread, which was
  right, but no test ever set that flag and the real gateway did not handle it. A fake that is
  more forgiving than the thing it stands in for hides exactly this kind of defect.

### Eighth look

The seventh look ran eight reviewers over the repository and only two of them finished; the
rest were cut off. This is the other six, and they went at the parts nobody had looked at yet:
what two things doing the same work at the same time do to each other, what a half-finished
delivery leaves behind, and what GitHub does that the code was never told about.

Three of these are damage the seventh look's own fixes did. Rebuilding a deleted thread was the
right idea, and it opened its own holes.

- **A permission refusal read as a deleted thread, and deleting a thread is now something the
  bot acts on.** `discord.Forbidden` and `discord.NotFound` were caught together and both
  reported as "gone". Since a thread that is gone gets rebuilt, an admin briefly denying the bot
  View Channel would have it forget the item's thread and open a fresh one, orphaning everything
  already mirrored into the original. The two are now told apart everywhere: gone means rebuild,
  refused means stop and say so. A channel that is gone or cannot hold threads is permanent as
  well, since both need somebody to run `/set_channel`.
- **An issue whose thread was deleted was still lost for good.** The rebuild lives in the write
  path, but an open issue unlocks its thread before writing, and that unlock ran on the dead id
  and raised first. Pull requests never unlock, so it worked for them and never ran for issues,
  which is why the tests missed it. The unlock now steps aside for a thread that is not there;
  the replacement is unlocked anyway.
- **A comment on a deleted thread was retried for two hours and then dropped.** The note mirror
  and the item sync sit either side of the same boundary and only one of them knew a dead thread
  can be rebuilt. A note that finds its thread gone now lets go of the id and asks to be tried
  again, and the item's next event rebuilds. Letting go is conditional on the id still being the
  one that failed, so a rebuild that has already happened is not undone.
- **The rebuild could give one item two live threads.** Clearing the thread pointer was
  unconditional, so it could wipe a claim another sync had just made, and then both callers
  claimed successfully. Attaching a thread is now a swap from the exact id the caller read, which
  covers first creation and rebuilding as one case and removed a whole step in the process.
- **The delivery timeout could cancel between creating a thread and recording it.** Sixty
  seconds is generous but discord.py sleeps through rate limits, and a dozen pull requests opened
  at once will reach it. The cancellation landed wherever the handler happened to be, and if that
  was inside the create call the thread existed in Discord with nothing pointing at it, so the
  retry opened another. Creating and recording now run as one unit that a timeout cannot split.
- **Redeploying parked a whole batch for five minutes.** Shutdown cancelled the worker outright,
  and the deliveries it had leased but not started stayed locked until their lease ran out, while
  the new process polled an empty queue. The worker is now asked to stop, finishes the delivery
  in hand, and hands the rest straight back without counting an attempt against them.
- **Two syncs of one item could ping the same reviewer twice**, and so could a delivery that
  failed after the ping went out. Who has been told was read first and written afterwards, which
  is the same read-then-write shape fixed three times elsewhere. The ping is claimed before the
  message is sent now, and handed back if the message does not go.
- **Re-requesting a review told nobody, which is the one moment the feature exists for.** GitHub
  drops a reviewer from `requested_reviewers` the moment they submit their review, and sends no
  `pull_request` event saying so. The ping fires when the assignment row is created, so the stale
  row survived and the re-request read as "already asked". The author's push does not help
  either: that arrives as `synchronize`, which is deliberately not handled. A submitted review
  now closes the request that asked for it, so a later re-request is a fresh one. The old test
  passed only because it injected an event GitHub never sends.
- **The retry budget was documented as two hours and was really thirty-six minutes.** Six
  comments quoted the figure and none of them matched the arithmetic: growth stops at the
  fifteen minute cap long before ten attempts add up to two hours. Attempts are now sixteen,
  which is what two hours costs, and the figure is computed from the settings rather than
  written down again, with a test holding the two together.

### Also in this pass

- **The shipped image was built from unpinned dependency ranges.** `uv.lock` is committed and
  CI installs from it, but the Dockerfile ran a plain `pip install .`, so the image got whatever
  PyPI served that minute. Three different dependency sets existed at once: tested, audited, and
  shipped. That also made the build provenance attestation describe something nobody could
  reproduce. The image is now built from the lock with hashes.
- **A version tag published a signed release image without running a single test.** CI did not
  trigger on tags and the release workflow did not depend on CI, so the two raced on `main` and
  on a tag nothing ran at all. Release is now a workflow CI calls once its checks have passed,
  which also keeps the tag ref pointing where the image tagging expects.
- `/pr` and `/issue` were the same fifty-seven lines twice, and every command carried its own
  error ladder covering a different subset. One error caught in only one of them was a silent
  failure in the others: the reply came after the interaction had been deferred, so the person
  who ran it watched a spinner. They now share one body, error replies come from one table, and
  the command tree has a handler so anything unexpected still gets answered rather than leaving
  somebody waiting. Both keep their own parameter names, `pr_link` and `issue_link`, because
  those are what somebody types and the requirements name them.

### The test matrix had never passed

`pyproject.toml` says `requires-python = ">=3.12"` and CI runs the suite on 3.12, 3.13 and 3.14.
Two of those three legs had been failing since the day they were added, and nobody had run them
anywhere the result was visible.

`PullRequestSnapshot.display_state` called `super().display_state`. Every snapshot is a
`slots=True` dataclass, and that decorator cannot add slots to a class in place: it builds a new
one and throws the original away. The bare `super()` reads its class from a cell captured at
compile time, which still points at the class that was discarded, so calling it raises
`TypeError: super(type, obj): obj must be an instance or subtype of type`. Python 3.14 fixed
that; 3.12 and 3.13 did not. Twenty-two tests failed on both, including every rendering test,
because rendering reads `display_state`.

Naming the class in the `super()` call resolves it when the method runs rather than when it was
compiled, so it finds the class that exists. The whole suite now passes on 3.12 and 3.13 as well
as 3.14, which is what the declared floor was always claiming.

Found by running `uv sync --python 3.12` rather than by reading, and the
suite against it, which is exactly what the CI job does and what nobody had watched it do.


### Ninth look

Four of the five things here were introduced by the seventh and eighth looks. New code is where
the bugs are, and code written to fix bugs is not exempt.

- **A reviewer could be told nobody was going to tell them.** The eighth look made the notifier
  claim a ping before sending it, and hand the claim back if the message did not go. Handing it
  back was guarded by `except Exception`, which does not catch cancellation, and cancellation is
  the likeliest way that post fails: the worker gives each delivery sixty seconds and cancels the
  handler where it stands, and where it stands is inside a Discord call that discord.py is
  sleeping through because of a rate limit. The claim survived, the retry found nothing to
  claim, and the ping was never sent by anyone. It now catches everything, and the hand-back is
  shielded so the same cancellation cannot interrupt that too.
- **The worker started leasing before the bot had connected.** Both tasks are created in the same
  breath at startup, but logging in and connecting to the gateway takes seconds, and until it
  finishes there is no session to make a Discord call on. Every delivery leased in that window
  failed against a client that was not ready and spent an attempt on a problem that fixes itself
  a second later. The worker now waits for the bot before its first batch. The endpoint does not
  wait for anything, which is the point of writing deliveries down.
- **The command error backstop could only ever say "something went wrong".** discord.py hands its
  handler whatever the command raised wrapped in a `CommandInvokeError`, and the reply table
  matched on the wrapper, which is not an error it knows. Every error reaching the backstop got
  the catch-all instead of the message written for it. It looks through the wrapper now.
- **The backstop could also raise.** An interaction that has expired, or been answered already,
  makes the reply fail, and a failing error handler is worse than none: discord.py logs a second
  traceback and the person who ran the command is still waiting. It no longer raises.
- **`GitHubRateLimitError.retry_after` carried a moment in time labelled as a number of
  seconds.** `retry-after` is a delay, but `x-ratelimit-reset` is the epoch second the window
  reopens, and both were being returned unchanged. Nothing reads the field yet, so nothing has
  gone wrong; whoever reads it first would have waited about fifty-six years. It is a delay now,
  worked out against GitHub's own `date` header rather than this machine's clock.
- **`/pr` and `/issue` still identified the repository by name.** The eighth look taught the
  webhook path to follow a rename, which fixed the mirror but not the commands: both compare the
  pasted link against the stored name, and that name is only corrected when a webhook happens to
  arrive. Between the rename and the next event, the correct link was still refused. They settle
  it on the numeric id now, which is what GitHub keeps across a rename, and only on the path that
  was about to refuse anyway, so a matching link costs no extra call.
- A dead branch in the status handling: 5xx and everything else unrecognised raised the same
  error with the same message, written twice.

### Tenth look

This one was run empirically: reviewers that built harnesses and ran them rather than reading.
Several of the findings are damage from the seventh, eighth and ninth looks, including one where
a fix was made and the setting that ships was left behind.

- **The retry budget was never raised where it counts.** The ninth look changed
  `WorkerSettings.max_attempts` to 16, which is what two hours costs, and added a test to hold
  the two together. `Settings.worker_max_attempts` stayed at 10, and that is the value the
  running worker uses, so the shipped budget was still thirty-six minutes. The test passed
  because it measured the dataclass default rather than the path production takes. Both are 16
  now, and the test measures the configured one.
- **A rebuild could destroy the thread it had just made and report success.** Attaching a thread
  swaps from the id the sync started with, and treats any other outcome as "somebody else got
  there first". But the pointer can also be empty, because the note mirror lets go of a thread it
  finds deleted. The rebuild then deleted its own healthy replacement, left the item with nothing
  at all, and still answered SYNCED, so the delivery was finished and the event lost. An empty
  slot is now taken rather than treated as a loss, and an item that has gone entirely is reported
  rather than dressed up as success.
- **A late delivery put an old repository name back.** The eighth look taught the sync to follow
  a rename, and it did that before checking whether the delivery was stale. Every payload carries
  the name as it was when GitHub sent it, so a delivery that arrived late reverted the rename and
  broke `/pr` again until the next event. The rename is now followed only for a delivery that is
  actually current.
- **The staleness watermark could move backwards.** An item with no thread is deliberately never
  treated as stale, because skipping there would leave it without one; that means an old snapshot
  can be applied, and it was overwriting the stored timestamp with its own. The next genuinely
  late delivery then passed the guard too. It is a high-water mark now.
- **Anyone who could comment on the repository could ping any Discord user.** Mirrored text is
  quoted, and the code said that stopped GitHub markdown restyling the thread. It does not:
  markdown renders inside a blockquote, and `<@1234>` resolves to a real ping, because Discord is
  told to honour user mentions and cannot tell the ones the bot built from the ones it is
  quoting. Titles and comment bodies are now neutralised before they go in, which also means the
  700-character preview can be cut anywhere without leaving a `**` open.
- **A signature header with a non-ASCII byte answered 500 instead of 401.** `compare_digest`
  only accepts ASCII and raises otherwise, and that header comes off the network from anybody.
- **The endpoint read a body of any size into memory before it could check anything.** The
  signature covers the whole body, so it has to be buffered first; the cap therefore has to come
  before the read. GitHub will not send more than 25MB.
- **`_remember` was the one write to the thread pointer without a compare-and-swap**, so a sync
  a step behind could point an item back at a thread that had already been abandoned.

### The tests that were not testing anything

One reviewer found these by breaking the code and seeing what still passed.

- `test_truncation_keeps_whole_lines` compared the truncated block with itself, so it passed
  whatever truncation did. It now checks the shape every surviving line must have, and a second
  test checks that the lines kept are the first ones rather than an arbitrary subset.
- Nothing exercised the worker's pruning, so the seven-day retention of private-repository
  payloads was only tested at the store. The loop that has to call it is covered now.

### Known and not fixed

- Two syncs of one item can still interleave their Discord calls. The database half is ordered
  by the staleness guard, and the worker handles deliveries one at a time, but `/pr` and `/issue`
  can run beside the worker, and the Discord phase happens outside any transaction by design.
  A reviewer reproduced a superseded snapshot locking a thread after a newer one had unlocked it,
  by injecting realistic latency into every Discord call. Closing it properly means holding a
  per-item lock across the Discord phase, which is a design change rather than a patch.
- A comment that arrives before its item has been tracked at all is still dropped rather than
  retried. The case where the item exists without a thread was fixed; this one is ambiguous,
  because most notes on untracked items are on things this bot will never track, and retrying
  them all would spend the full budget on every one.

### Eleventh look

Six reviewers, no verification stage, because the two passes before this lost every verifier to
a session limit while the finders came back fine. Three of them independently found the same
defect, which was introduced by the ninth look.

- **A Discord login that failed parked the worker for the life of the process.** The ninth look
  had the worker wait for the gateway before its first batch, which was right, but
  `wait_until_ready` waits on an event that is only ever set by a connection that succeeds. A
  bad token left it waiting forever: the endpoint went on accepting deliveries, the queue grew,
  pruning never ran, and nothing said why. It now waits for the connection or for the bot to
  stop trying, whichever comes first, and stops loudly in the second case. The deliveries stay
  pending for a process that can actually reach Discord, rather than being burned through.
- **A comment was posted again on every retry of its delivery.** The eighth look added a step
  after the note was mirrored, to close a fulfilled review request. A retry re-runs the whole
  handler, so anything failing after the post put the same comment in the thread a second time.
  The recoverable database work now happens first and the Discord post last, which is the order
  the rest of the project already uses. Closing a review request twice costs nothing.
- **`/pr` and `/issue` accepted numbers that are not numbers.** `str.isdigit` is true for a
  great deal more than 0-9. An Arabic-Indic digit converted silently, so a link ending in one
  synced a different pull request than the one somebody pasted; a superscript or a circled digit
  passed the check and then raised on conversion, escaping as an unhandled error. This is the
  same shape as the `/pr [` crash from the fourth look, in the same function.
- **A refusal could be too long to send.** Several replies quote back what was typed, and a
  slash command argument can be longer than a Discord message may be, so an over-long argument
  made the refusal itself fail and the person saw nothing at all. Replies are trimmed centrally
  now rather than at each call site.
- **A failing prune ran every couple of seconds instead of once an hour**, because the timer was
  only moved forward when it succeeded.
- **The lease was half of what a batch can take.** Ten deliveries each allowed sixty seconds is
  ten minutes; the lease was five. A second replica could take deliveries this one was in the
  middle of. It covers the batch now.
- **A hard cancellation left the rest of the batch locked.** The cooperative stop hands back
  what it has not started, but a cancellation that lands mid-delivery skipped that entirely.

- **The ping claim had a gap of its own.** The ninth look moved the claim before the message
  and handed it back on failure, but a cancellation could land between the claim committing and
  the guard that hands it back, which is exactly where cancellation is delivered. The claim is
  shielded now. The hand-back itself is asynchronous under cancellation, by design: the await
  returns at once and the release lands a moment later, well inside the five seconds before the
  delivery is retried. A test that asserted it synchronously was flaking about one run in five,
  and was asserting an ordering the code never promised.

### The locking window, mitigated rather than closed

Two syncs of one item can still interleave their Discord calls: `/pr` runs beside the worker and
the Discord phase is outside any transaction by design. The step where that actually hurt is
locking, because it is last and it is decided from a snapshot that may already be superseded, and
because a reopened issue left in a locked thread does not right itself the way a stale metadata
block does. That step now re-reads the item first and stands down if a newer sync has been
through. The metadata window remains and is self-healing. A per-item lock held across the Discord
phase would close both, at the cost of pinning a pool connection for every sync and making `/pr`
wait on the worker; that is a design decision rather than a patch.

### Found and not acted on

- Pull request priority is never read from GitHub labels, so every PR thread reads
  `Priority: UNSET` while an issue with the same labels reads `HIGH`. The parser exists and the
  issue path already calls it. This is a behaviour change to a documented field rather than a
  defect in isolation, so it wants a decision first.
- A review requested from a GitHub team pings nobody: `requested_teams` is never parsed, and
  there is no GitHub-team to Discord-role mapping in the schema at all. That is a feature.
- The permissions table in requirements.md does not grant the Reviewer tier `/pr` and `/issue`,
  and the code does. Widening, not narrowing, but the two should be made to agree.

### Twelfth look

Six lenses aimed at operating this rather than reading it; three came back before the session
limit. Two of their findings were the unfinished half of the eleventh look's own fix: that pass
turned a worker that hung silently into one that died silently, which is better, and still not
something anybody would notice.

- **The process reported healthy with its worker dead.** A rotated Discord token kills the worker
  task; one ERROR line goes past and the container carries on. The healthcheck was a TCP connect
  to the API port, which proves only that uvicorn is listening, so `restart: unless-stopped`
  never fired, GitHub saw 200 for every delivery, and the queue grew with nobody looking at it.
  There is a `GET /health` now reporting whether the worker is alive and the database reachable,
  and the container healthcheck asks it rather than the socket.
- **Startup never touched the database.** Building an engine connects to nothing, so a wrong
  password or a database that had never been migrated still reached "startup complete" and passed
  a health check, with every delivery accepted and then failing behind it. Startup now proves the
  database is there and migrated before the port opens, and says which setting to look at when
  it is not.
- **GitHub's Redeliver button did nothing.** It sends the same delivery id, so a delivery this bot
  had already given up on came back as a duplicate and was answered without anything happening.
  That button is the obvious thing to press once whatever broke has been fixed, and the only
  alternative was hand-written SQL against the queue. A redelivery of a FAILED delivery now puts
  it back. A repeat of one already processed is still a duplicate.
- **Nothing enforced the lease invariant.** Two comments state that the lease has to cover
  batch size times the delivery timeout, and nothing checked it, so raising the batch size for
  throughput would quietly let a batch outlive its own lease and hand rows still in flight to
  another replica. Settings refuses that combination now, at startup, naming all three.
- **The likeliest explanation for "the bot has stopped posting" was logged below the level that
  ships.** A webhook installed across an organisation, or a repository whose registration has
  gone, files every delivery as IGNORED with no reason recorded and said so only at DEBUG while
  the default is INFO. One of the three branches said nothing at all. All three are at INFO now
  and name the repository and the event.
- **docker compose overrode the database URL the operator set.** `environment:` wins over
  `env_file:`, so the first line of .env.example had no effect under compose, and the whole
  stack agreed with itself. The literal is a default now.

### Decisions taken rather than found

Two long-standing disagreements between requirements.md and the code, settled by the owner.

- **Pull request priority now comes from GitHub labels**, exactly as an issue's does. A pull
  request labelled `high priority` used to render `Priority: UNSET` on the line directly above
  the label that set it, while an issue with the same label read `HIGH`. The rule lives on the
  shared snapshot now rather than on one of the two subclasses.
- **Reviewers no longer have /pr and /issue**, which is what the permissions table says. Somebody
  who is a reviewer and also a developer or project manager keeps them: holding any listed role
  is what grants a command, rather than holding only listed ones. That was already how the gate
  worked, and there are now tests saying so.

### Thirteenth look

The three lenses the twelfth look lost to a session limit, resumed. Two of the four defects were
in code written earlier the same day, and one of them was a fix that caused the thing it claimed
to prevent.

- **The shield on the ping claim guaranteed the lost ping it was added to stop.** `asyncio.shield`
  protects the coroutine, not the caller's await: cancelling meant the await raised at once while
  the claim carried on and committed, so the variable holding who had been claimed was never
  bound, the guard that hands the claim back was never entered, and the ping was owed to nobody
  for ever. Unshielded, the same cancellation aborts the transaction before it commits and
  nothing was claimed, which is the outcome worth having. The reasoning that put it there was
  wrong in a way worth naming: there is no await between the claim returning and the guard, so
  there was never a gap to close.
- **/health called a process healthy with its gateway dead.** It watched the worker and not the
  bot. The worker waits for Discord once, before its first batch, so a gateway that dies after
  connecting, a rotated token or a revoked intent, leaves the worker leasing happily while every
  Discord call fails. That is precisely the case the endpoint was added for a day earlier, and it
  reported 200 through all of it. It reports the bot now, and says which half failed.
- **/health opened a database connection on every request.** The route is public and
  unauthenticated, so a flood of requests could exhaust the pool the worker runs on, which is the
  very thing the endpoint exists to notice. The probe is reused for five seconds; the container
  asks every fifteen.
- **Shutdown skipped every cleanup step once the worker had died.** Stopping a task adopted its
  exception, and stopping the worker is the first thing shutdown does, so a worker that ended in
  the designed way, a bad token, took the Discord client, the HTTP client and the database pool
  down with it unclosed. Each step is now guarded on its own, and stopping a task watches it
  rather than awaiting its result.

### The queue's indexes, measured rather than assumed

Both of these were found by filling a database with 200,000 deliveries and reading the plans.

- **The lease had no index it could use.** The one meant to serve it led on `status`, and the
  rest of the predicate is `next_attempt_at IS NULL OR next_attempt_at <= now()`, which no index
  can answer as a condition. While the queue is nearly empty `status` alone is selective enough
  and nothing looks wrong. Once a few hundred deliveries are backing off the planner decides the
  primary key is cheaper and walks the whole table: 24,637 buffers and 126 ms to return nothing,
  every two seconds, getting worse as the retention window fills. Migration `0005` replaces it
  with a partial index over the live rows, which is what the predicate can actually prove.
  Measured on the same 200,000 rows: **13 buffers**.
- **Pruning read every row to find the few past the retention window**, and paid the same full
  scan in the hours it deletes nothing, which with a seven-day window is most of them. It is
  indexed now: **3 buffers, 0.2 ms**, from 22,902 and 73 ms. The delete was also asking the
  database to hand back every deleted primary key, for a session holding none of them.

`shannon/main.py` had no test coverage at all before this, which is part of why three of these
lived there. It has some now.

### Fourteenth look

Aimed rather than broad this time, on the grounds that thirteen passes have worked the original
code over and the risk has moved to what the review itself keeps changing. The fan-out hit a
session limit and returned nothing, so this is one finding, found by reading.

- **The `/issue` service path had never been exercised.** `FakeGitHubClient` implemented two of
  the three methods the `GitHubClient` Protocol declares, and the missing one was `get_issue`.
  A Protocol is structural and unchecked at runtime, so nothing ever complained: the fake simply
  could not drive the issue path, so no test was written that would have needed it, and
  `get_issue`, `build_issue_sync` and `manual_issue_sync` appeared nowhere in ten thousand lines
  of tests. `/pr` was covered throughout. Its twin had nothing.

  The path turns out to work. This was a
  test gap and not a defect, and the fix is the tests that were missing, plus the method on the
  fake that made them impossible to write. `HttpGitHubClient.get_issue` went from an entirely
  unexecuted body to 96% covered, including the case that matters most, GitHub answering the
  issues endpoint for a pull request, which must not be tracked as an issue.

  It stayed hidden because a fake more capable than nothing and less capable than the
  real thing does not fail, it silently narrows what anyone can test. The eighth look found the
  same shape in the thread gateway fake, where a comment described behaviour the fake did not
  have. This is the second time.

### Fifteenth look

Three things carried forward from earlier passes and never acted on, because each was recorded
as "found and not fixed" rather than resolved. All three are defects rather than the feature
requests they were filed alongside.

- **/set_channel said threads had moved when nothing moves.** It answered "Moved from
  <#old>", and Discord cannot move a thread between channels: every item already tracked keeps
  the thread it has, and only new ones appear anywhere different. An admin running the command
  to tidy a server would go looking for threads that never went anywhere. It now says what
  actually happens, which is the part they need: "Issues for owner/repo will now appear in
  <#new>. Threads already open stay in <#old>."
- **The worker's log lines named nothing an operator could act on.** All three of them, the
  retry, the give-up and the permanent failure, carried a GitHub delivery id and an exception
  message. Told that delivery `4f2a...` cannot be handled because Discord will not let the bot
  create a thread, there is no way to know which repository or which channel to go and fix
  without finding the row and decoding its payload, and the row is gone once it ages out. Every
  one of them now names the event, the repository and the item number.
- **A policy handed the wrong kind of snapshot filed it silently.** Nothing but the wiring pairs
  `PullRequestPolicy` with a pull request, and a mismatched pairing would store the item under
  the wrong type and read fields the snapshot may not carry. It fails once and loudly now,
  rather than being retried for two hours: it is a wiring mistake and no payload can cause it.
  MVP 4 adds a third object type, which is when this becomes easy to get wrong.

### Sixteenth look

A different method, because the hunting was giving less each time: take the coverage report,
list every line of production code the suite never executes, and read each one to decide whether
it is wrong or merely untested. Unlike a search, that list is finite and it ends.

The answer, mostly, was untested:
after fifteen passes the uncovered lines are almost all error and edge branches, and reading them
found no new defect in any of them. What it did find is that the branches guarding the subtlest
behaviour in the project had nothing watching them.

- **The two branches that settle a rebuild racing a cleared thread pointer had never run.** They
  are the narrowest race here: a rebuild swaps from the dead thread id it started with, and the
  note mirror can let go of that same id while the rebuild is in flight. Both branches turn out
  to be correct. Neither was exercised, so either could have been broken by any later edit
  without a single test noticing. They are covered now, by a gateway that clears the slot at the
  moment the replacement is being opened, and one that deletes the item outright.
- **The worker's hand-back on an outright cancellation is best effort, and now says so.** Awaiting
  anything from inside a cancelled task returns at once, so it starts the hand-back and does not
  see it finish. It usually lands, because shutdown waits for the task and the loop is still
  running; if the loop stops first the rows wait out their lease, which is where they would have
  been anyway. The comment claimed more than that. The cooperative stop is the path that does it
  properly and the one taken almost every time.
- Also covered: the review ledger's two early returns, for a review by a deleted account and one
  on a repository nobody registered.

### Seventeenth look

Two passes of hunting had turned up nothing new, so this one spent its time on the other half of
the job: making what is already here easier to work on.

- **The test suite emitted a thousand warnings a run, and they were all the same one.** discord.py
  2.7 passes `re.sub`'s count positionally, which Python 3.13 deprecated; it is their code and
  there is nothing to do but wait for a release. Left alone it was a thousand lines per run,
  which is more than enough to bury a warning that does matter. That one message is filtered by
  name, with the reason written down. Everything else is untouched, and a deprecation warning
  from our own code still fails the suite outright, which was already the case and is worth
  keeping.
- **The lifespan had no tests, and it is where the last three defects in this project lived.**
  Startup ordering, the database check that has to fail before the port opens, the readiness
  gate, and the shutdown sequence were all covered only by reading. `shannon/main.py` went from
  53% to 92%; what is left is the process entry points. The tests that matter most are the two
  that pin the defects: a gateway that never connects is reported unhealthy rather than fine,
  and a worker that has already died does not stop the Discord client, the engine and the HTTP
  client from being closed.

  Writing them turned up one thing: a database that refuses the connection
  surfaces as `OSError`, not as anything SQLAlchemy wraps. The lifespan catches broadly and
  reports, which is right, but it is the sort of assumption that is easy to get wrong when
  narrowing an except clause later.

Nothing in this pass fixes a defect, because none was found. That is the result.

### Eighteenth look

Every defect here is a race, and every one of them was found by asking the same question of a
different piece of code: what happens when two of these run at once. The answer had been worked
out carefully for some paths and never asked of others.

- **`/health` reported a healthy process as down whenever two checks landed together.** The probe
  is cached so a public endpoint cannot open a connection per request, but the cache was stamped
  fresh on the way *in*, before the connection was awaited. Every caller arriving during that
  window was handed the cached value before anything had set it, which on the first probe is its
  initial `False`. Two probes at once is not an exotic case: it is what an orchestrator running a
  liveness and a readiness check does, and the answer it gets back is the one that decides whether
  to restart the process. So the endpoint added to catch a wedged process was itself the thing
  most likely to kill a working one. The stamp now happens once there is an answer, a lock means
  one probe runs at a time rather than the whole burst opening a connection each, and the probe
  has a deadline so a database that accepts the socket and then goes quiet cannot park every
  health check behind it. The existing tests all awaited the probe one call at a time against a
  fake that connected instantly, so there was never an await point for a second caller to arrive
  in. The fake can be slow now.
- **Two syncs of one item both adding the same person collided on the unique constraint.** The
  assignment store read the existing rows and then inserted the difference. That is safe only if
  nothing else is syncing the same item, and three things regularly are: `/pr` runs on the bot's
  task while the worker is mid-delivery, GitHub sends several events at once for one item, and a
  second replica leases in parallel by design. The loser of the race took the whole sync down.
  It survived a file named `test_concurrent_sync.py` because every test in it used an
  item that did not exist yet, and a new item is serialised by the upsert in `get_or_create`, so
  the loser blocks until the winner commits and then sees the winner's rows. Nothing serialises
  an item that already exists, which is the case `/pr` is for. The insert settles its own
  conflict now, doing nothing rather than overwriting, because the row the other caller wrote is
  the row this one was about to write and it may already hold a claimed `notified_at`.
- **A double-submitted `/link` came back as a raw database error.** Both halves of a link are
  unique within a guild and either can be held by a different row, so the store clears both out
  and writes the pairing fresh; two of those overlapping have both find nothing to clear and both
  insert. The loser now retries instead of raising, which lands cleanly because the rollback left
  the other attempt standing. Last writer wins, which is what replacing whatever either side had
  already meant.
- **A failure closing the HTTP client left the database pool open.** `Container.aclose` closed
  the client and then disposed the engine, so anything thrown by the first skipped the second.
  This is the same shape as the shutdown bug fixed two passes ago, in the code that shutdown
  calls. The engine goes in a `finally`.

### The fake that keeps being narrower than the real thing

Three times now a stand-in has offered less than the thing it replaces, and not once did it fail
a test. It quietly removed a path from what the suite could reach, which is worse than failing,
because the suite went on reporting green over the hole: a thread gateway whose docstring
described behaviour it did not have, a GitHub client with no `get_issue` so nothing could drive
`/issue` at all, and a lifespan container standing in for seventeen attributes with three. The
third was written one pass after the entry warning about the second.

Protocols are structural and there is no type checker in this project, so nothing was ever going
to catch this by itself. Now something does. `tests/unit/test_stand_ins_match_what_they_replace.py`
walks every protocol's members by reflection and checks each implementation offers all of them
with the same argument names, covering the real implementations as well as the fakes, since
neither was being checked. It was run against both historical bugs before being kept: the missing
`get_issue` and a renamed parameter are both caught. The lifespan container is gone, replaced by
the real one with only its worker swapped.

### Nineteenth look

Same lens as the last pass, pointed at the paths it had not reached yet.

- **An older sync could push the staleness high-water mark backwards.** `github_updated_at` is
  what tells a late delivery from a current one, and it was raised with a comparison done in
  Python against a value read at the top of the transaction. That read is not the row at the
  bottom of it: two syncs of one item overlap by design, both read the mark before either
  commits, and whichever commits last writes what it worked out from a read that is by then out
  of date. If it is the one carrying the older timestamp, the mark drops. The next genuinely late
  delivery then reads as current, and the lock step decides from this same field whether it has
  been superseded, which is the step whose mistakes do not right themselves. The comparison is
  `GREATEST` in the update now, so it is made against the row as it stands at write time, and it
  has moved to `TrackedItemStore.raise_updated_at` where the rest of the item's SQL lives and
  where it can be tested on its own.

  The first test written for this was worthless: six gathered syncs with
  descending timestamps, which passes on the broken code. Once the first one commits the
  staleness guard turns the other five away before they ever reach the write, so the interleaving
  that breaks it never happens. The test that replaced it holds two transactions open and commits
  them in the order that does the damage.

- **The webhook body size limit did nothing unless the sender volunteered its size.** The cap was
  applied to `Content-Length` and then the body was read whole with `await request.body()`.
  Nothing obliges a client to send that header, so a chunked request without one passed a check
  on a header that was not there and was read to the end whatever its size. The endpoint has to
  be reachable from the internet for GitHub to use it, and no secret is needed to do this, because
  the signature covers the body and cannot be checked until the body is in hand. Measured before
  fixing: 40MB went into memory and came back 401. The bytes are counted as they arrive now. The
  declared size is still checked first, since it costs nothing and turns away an oversized
  delivery that is honest about itself without reading any of it.

Also looked at and found sound: the thread claim's compare-and-swap and its two-step recovery
when the slot is emptied mid-flight, the permission gate's role matching and additive resolution,
and the lease duration against the worst case batch. One thing noted and not acted on: `/pr` for a
repository deleted and recreated under the same name reports "no channel mapped", which is
misleading, but the path needs a repository to be destroyed and rebuilt with its name intact.

### Twentieth look

Both defects here were found by running the thing rather than reading it, which is the first time
that has been true in this project.

- **Every clean shutdown logged the line that means the process is dead.** Booting the real
  application end to end, rather than the lifespan with fakes in it, put "the delivery worker
  stopped without an error" in the output of a shutdown where nothing had gone wrong. The done
  callback exists to catch a background task dying on its own, and it could not tell that from a
  task being told to finish, because a clean shutdown ends both of them exactly the way a failure
  does: the worker's loop returns when it is stopped, and the Discord client's start returns once
  it is closed. So the one message meaning this process is now useless was printed most often at
  the moment it meant nothing, which is the fastest way there is to teach everyone to skip it.
  The lifespan now says whether it asked, and the callback stays quiet when it did. An error is
  still reported either way, because being on the way out is a reason to expect the exit, not a
  reason to stop reading the error.
- **A label name could carry a working mention into the thread.** Every other piece of
  GitHub-authored text that reaches a Discord message goes through `as_plain_text`, which puts a
  zero-width space inside `<@1234>` because that is, as the module already says, the one mention
  form that can still ping somebody. Label names went out wrapped in a markdown code span
  instead. A code span is a rendering, and `allowed_mentions` is not reading renderings: it is
  the delivery gate, it is told to honour user mentions, and it reads the content it is handed.
  Labels are named by anyone with triage rights, so this is a smaller door than a comment body,
  but it was the only untrusted field in the module relying on the wrong layer. The defusing is
  its own function now and both callers use it, and a label with a backtick in it still renders
  the way it did.

Checked by running it and found sound: the migration chain applies from nothing to head, reverses
to base leaving only `alembic_version`, and applying it a second time produces a byte-identical
schema. `@everyone`, `@here` and role mentions cannot ping from any field, because
`allowed_mentions` refuses them outright. Titles and comment bodies were already defused. An
author's login cannot carry a mention at all, since GitHub logins are letters, digits and
hyphens. A title does reach the thread name undefused, and that is fine: thread names are not
scanned for mentions.

Also learned, the hard way, twice: two pytest sessions pointed at one database corrupt each
other, because the session fixture rebuilds the schema with `drop_all` before `create_all`. Test
failures that appear only while something else is running are that, and not the code.

### Twenty-first look

Carrying on with running things rather than reading them. Most of this pass is verification that
found nothing, which is worth writing down as plainly as the one defect.

- **A link could steer the bot's API call to a path nobody built.** `.` and `..` both match the
  repository name pattern, and neither is a name GitHub will ever create. The name goes straight
  into the path of a request made with the bot's token, and the HTTP client collapses dot
  segments before sending, so what leaves is not what the caller wrote: `/pr` on
  `github.com/owner/../pull/7` builds `/repos/owner/../pulls/7` and sends `/repos/pulls/7`, and
  `/register` on `github.com/owner/..` sends a bare `/repos`. Nothing private is reachable, because
  the owner pattern has no dots in it so there is only ever one level to climb, and both parsers
  already refuse anything that is not github.com. It is still a request the code did not make.
  Both names are refused now, next to the checks that were already being done on the rest of the
  link. A repository with dots in its name is untouched, since only the two that mean something
  to a path are refused.

Run against the code and found sound:

- **The webhook parsers do not break on a malformed payload.** Every field of every real payload
  was deleted, nulled, emptied, and replaced with each of a dict, a list, a list of nulls, a bool,
  a float, a 64-bit integer, a negative number, an empty string, a hundred-thousand character
  string, and a run of unicode direction marks. 3,376 mutations, and every one came back with a
  snapshot or with None. This matters more than it sounds: a parser that raises is not a crash,
  it is sixteen retries across two hours and then an event lost for good.
- **Nothing gets a live mention past the defusing.** 16,000 generated inputs through
  `as_plain_text` and `defuse_mentions`, including text drawn only from the characters that could
  plausibly build or break a mention, and not one live `<@id>` came out the other side.
- **The worker's settings mean what the comments say.** The shipped values and the dataclass
  defaults agree field for field, a delivery is held 2:06:15 before being given up on, which is
  the "roughly two hours" quoted in three places, and a worst-case batch of ten deliveries each
  running to its one minute timeout takes ten minutes against a fifteen minute lease.
- The adversarial link corpus is otherwise refused as it should be: a userinfo host
  (`github.com@evil.com`), a lookalike host, a non-http scheme, an issue link handed to `/pr`, a
  zero or negative number, and an Arabic-Indic numeral.

### Twenty-second look

Both of these came out of a review pass that was killed part way through. One finder had finished
before it died, nothing had verified its findings, and both turned out to be real once checked.

- **Markdown glued to a link went out unescaped.** `as_plain_text` exists so GitHub-authored text
  cannot restyle the thread it is quoted into, and `escape_markdown` takes an `ignore_links`
  argument that defaults to true: it skips whatever its URL pattern matches, and that pattern
  runs to the next space. So a comment ending `https://example.com/``` ` kept its fence, opened a
  code block nothing closed, and swallowed the rest of the message including the link back to
  GitHub. A title ending `https://a.com/**` left an odd number of bold markers, and bold runs
  past a newline, so every field label below it traded its emphasis with the value beside it and
  the block could be made to read however the title's author liked. Anyone who can open an issue
  on the repository can write either. The escaping no longer makes an exception for links.

  It is paid for: a URL with an underscore in it now comes out escaped and
  stops being clickable inside a quoted body. That is the right way round. A quoted comment is a
  preview and a pointer rather than a copy, the real link sits at the bottom of the note where
  nothing escapes it, and the metadata block has a link field of its own.

- **A renamed repository could not be reached at all.** GitHub answers 301 for a repository or
  owner that has been renamed, and for an issue moved to another repository; both are documented
  and ordinary. httpx does not follow redirects unless told to, and the status handling has cases
  for 401, 403, 404 and 429 with everything else falling through to "GitHub could not be
  reached". So `/register` on the old link failed permanently with a message about an outage that
  was not happening. `/pr` was worse: after a rename the stored name is stale, so the guard that
  exists precisely to settle a renamed repository by its id saw the stale name match, skipped
  itself, and asked GitHub for the old name, which meant `/pr` and `/issue` stayed broken for
  that guild until a webhook happened to arrive and correct the name. That guard also caught only
  a 404, so it could not have helped even when it did run. Redirects are followed now.

  Checked before turning it on, because the client carries a token: httpx drops the Authorization
  header when a redirect crosses to another origin and keeps it when it does not, which is the
  behaviour this needs.

Neither finding had been verified when the pass died, so both were reproduced here before being
acted on, and each fix has a test that fails without it. The redirect fix needed two tests rather
than one: every other test in that file injects its own HTTP client, so the behaviour test would
have passed with or without the change, and only a test that inspects the client the class builds
for itself actually pins the decision.

### Twenty-third look

- **The `/link` fix from the last pass was wrong, and its own test caught it.** Retrying once when
  the insert lost a race is enough for two callers and nothing more: with three, the two retries
  collide with each other rather than with the original winner, and the second collision has no
  retry left. It failed about one run in four, and only showed up on a machine busy enough to
  spread the scheduling out. Retrying was the wrong shape for it. Both halves of a link are
  unique within a guild, a row can conflict on either, and `ON CONFLICT` settles one constraint,
  so no single statement can do this: it needs the clearing and the writing to be one indivisible
  step. That is a per-guild advisory lock, taken for the length of the transaction, which linking
  in another server never waits on. The retry is gone, because with the lock it could not fire.
  The test now uses eight callers rather than three and fails every run without the lock instead
  of one in four.
- **A worker still waiting for Discord never noticed it had been told to stop.** The stop was a
  flag, and the loop that reads it had not started: the wait for the gateway had nothing to
  interrupt it, so the only thing that ended it was the shutdown grace running out and the task
  being cancelled. Every restart before Discord answered sat out the full five seconds and was
  then killed. A gateway that is slow, refused, or misconfigured is exactly when a restart is
  most likely, so this was at its worst in the case it was most likely to meet. The stop is
  published as something waitable as well as a flag, and the wait now finishes on either. A
  gateway that failed outright is still reported rather than being read as an ordinary stop,
  which is checked by its own test because the two are one line apart.

### Deliberately failing Discord part way through

`tests/integration/test_flaky_discord.py` fails a fixed fraction of every gateway call rather than
one arranged call, so failures land at different points inside deliveries that have already done
part of their work. At every rate tried the invariants hold: one item keeps one thread with none
abandoned beside it, the item points at a thread that exists, nobody is pinged twice, and nothing
is left half-finished in the queue.

Two things came out of writing it. At a fifty per cent failure rate a reviewer really is left owed
a ping, because the delivery ran out its sixteen attempts and was given up on, which is the end of
that road by design rather than a defect; that claim lives in its own test where the gateway
recovers. And the test was checked by breaking what it guards: with the ping claim no longer
stamping `notified_at`, it reports seven pings where it expects one.

### Mirroring a note twice

The delivery queue is at-least-once on purpose, and only some of it was written that way.

A delivery whose status write fails after the handler has already succeeded stays leased, comes
back when the lease runs out, and is handled again from the top. Every handler but one survives
that: syncing an item upserts its row, swaps the thread pointer from the id it read rather than
writing over whatever is there now, and claims a ping before sending it. Mirroring a comment or
a review had none of it. It posted, kept no record of having posted, and so posted again.

Reproduced against a live database before anything was changed, by failing the status write once
and letting the lease expire: the same comment appeared in the thread twice.

`mirrored_notes` is the record, added by migration `0006`. It is claimed before the post and
handed back if the post does not land, which is the shape `item_assignments.notified_at` already
uses for pings and for the same reason: recording it afterwards leaves the identical gap one step
further along. Handing it back matters as much as taking it. Without that, a note that failed to
send would be marked as sent, every retry would read it as done, and the note would be lost
silently, which is worse than the duplicate this prevents. Both hand-back paths have tests, the
ordinary failure and the one where the thread turns out to have been deleted.

The key carries the kind, `comment:123` or `review:123`, because GitHub numbers the two
separately and they do collide. Keyed on the number alone, whichever arrived second would be
taken for one already posted and dropped. That has a test too, with a comment and a review
deliberately given the same number.

Nothing is backfilled. Every note already in a thread has been through the queue, so an empty
table costs one claim each and nothing is at risk of being posted twice.

Two of the four tests fail without the fix. The other two cover the hand-back, which cannot fail
before there is a claim to hand back.

### A review request that came back from the dead

GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
`pull_request` event saying so, so the ledger followed the review and closed the request by
deleting the assignment row. A later re-request then inserted a fresh row with a null
`notified_at`, and the reviewer was asked again. That is the one moment the feature exists for.

Deleting it was too much for a queue that retries. A `pull_request` delivery whose Discord step
failed is retried with the payload it was captured with, and that payload still lists the
reviewer. Retried after the review, it found no row, inserted one, and posted `Review requested
from monalisa.` directly underneath `**monalisa** approved this pull request`. The row then
existed again with `notified_at` set, which is precisely the state the ledger exists to prevent,
so the next genuine re-request found it already there and told nobody for the life of the pull
request. Reproduced end to end before anything was changed, and the second half is the worse
half: the odd ping is noise, the swallowed re-request is the feature not working.

The row is kept and stamped with the time of the review instead, in GitHub's clock rather than
ours, because what it gets compared against is a timestamp out of a GitHub payload. A request
older than the stamp is a delivery catching up and is left alone. One newer is somebody clicking
re-request, and clears both stamps so the ping can happen again. The ping claim also skips a
fulfilled request outright, which is what stops a reviewer being asked to look at something they
have already approved when the original ping never made it out.

**This rests on GitHub advancing `pull_request.updated_at` when a review is requested.** It does,
because requesting one changes the pull request, but it is an assumption rather than something
this codebase can prove, so it is written down here and in the test that depends on it. One
existing test had to change with it: it sent a re-request carrying a timestamp from before the
review it was supposedly answering, which cannot happen in a real sequence. If the assumption
ever turns out to be wrong, that test is where it will show, and the answer is to find another
way to tell a re-request from a straggler rather than to loosen the comparison.

### Losing a thread stopped meaning losing the item

An item with no thread is deliberately never treated as stale, because skipping there would mean
it never gets one. That bypass was doing more than it was meant to. It said "build the thread
whatever the age of this delivery", and the code behind it also read "and believe everything else
this delivery says", so a payload captured before the item was last changed put back the old
title, the old state, the old status and priority, and swapped the people for whoever was on the
item at the time. That deletes assignees added since and pings ones removed since, in a thread
that has only just been rebuilt.

Reproduced both ways round before changing anything, which is what showed how narrow the trigger
looks and how ordinary it actually is. With the thread intact the old delivery is refused. With
the thread pointer cleared, the same delivery reverted the title and pinged a reviewer who was no
longer on the pull request.

The two questions are separate now. `is_superseded` asks only whether this payload predates what
is stored; `is_stale` is that plus having a thread to leave alone. A superseded delivery for an
item that has lost its thread still builds the thread and touches nothing else. Its metadata block
goes up out of date and the next delivery corrects it, which is a window rather than damage. The
people are what mattered: a ping cannot be taken back, and a reviewer deleted from the row is a
reviewer nobody is ever told about again.

The thread pointer is cleared in more than one way, so this is not a corner: the note mirror lets
go of a thread somebody deleted, and an item whose first thread creation failed never had one
while its row was already committed.

### Threads that reached Discord and nothing else

Two ways a thread could end up in a channel with no row pointing at it. Nothing anywhere
reconciles those: `discord_thread_id` is only ever read off a row found some other way, and the
index on it was dropped in migration `0004`, so an id that never reached a row cannot be reached
by anything at all. The retry finds no thread, opens a second one, and the first sits there taking
no comment, review or ping for the rest of its life.

- **Shutdown walked away from a thread it was in the middle of opening.** Creating and claiming
  are shielded together precisely so a deadline cannot land between them, and the shield was not
  enough on its own. It keeps the inner work running, but the caller's await raises at once, so
  the worker task ended, shutdown read that as the worker having stopped, and the engine was
  disposed and the loop closed with the claim unfinished. The shield was doing its half; nothing
  was doing the other half, which is waiting. The cancellation is caught now and the work waited
  for, bounded so a gateway that has stopped answering cannot hold the process open, and then
  passed on exactly as before. Worth being accurate about the shield: it is not what caused this,
  it is what made it fixable, because the work survives as something that can still be waited for.
- **A claim that could not be written left the thread behind.** Which half failed is what makes
  this recoverable: Discord answered or there would be no thread to worry about, so the database
  is the part that is down and the call that undoes the thread is the one still working. It is
  taken back now, which leaves the retry starting from where it thinks it is. Safe to do because
  nothing is claimed at the point it can fire, since the swap either commits and returns or
  matches nothing and commits nothing, and the one path that does raise after tidying up is
  allowed through untouched.

Both are narrower than they first look and neither is exotic. The first needs a shutdown while
Discord is slow, which is what a rate limit does, because discord.py sleeps through those rather
than failing. The second needs the database gone for the moment between two calls, which
`pool_pre_ping` already absorbs when it is only a stale pooled connection.

The test that pins the first one had to be made to cancel at the right instant. The first version
slept and cancelled during the database work instead, well before the thread existed, and proved
nothing while passing.

### The queries, and a shield that said too much

The schema and query lens was the one review pass that never ran, so it was done here by hand
against a populated database rather than by reading. It found nothing, recorded here as plainly
as a defect would be.

Every id that comes from Discord or GitHub is `bigint`; only the internal keys and the pull
request number are 32-bit, which is what they should be. On 200,000 tracked items, 200,000
assignments, 200,000 mirrored notes, 100,000 links and 200,000 deliveries, every query the
services actually run goes through an index: the item lookup by number, the assignment lookup by
item and role, the ping claim, the link resolution, and the queue lease, which still uses the
partial index from `0005` and touched 22 buffers to take ten rows.

One thing found and fixed, in code written earlier the same day:

- **A failure while shutting down was reported by asyncio instead of by us.** Opening a thread ran
  under `asyncio.shield`, and a shielded future whose caller has been cancelled has its exception
  reported by asyncio in its own words, which says less than we can and arrives during shutdown,
  where the log is the only thing anybody has. Running the work as a plain task and waiting on it
  protects it exactly as well, and leaves the failure ours to report. Checked both ways: with the
  shield, the loop's exception handler reports `RuntimeError exception in shielded future` and
  nothing else; without it, the log says which item, that it was shutting down, and what went
  wrong, and the loop reports nothing at all.

  How it went: the first attempt kept the shield and retrieved
  the inner exception, on the assumption that retrieving it was what stopped asyncio complaining.
  It was not, and the comment written to explain it was wrong; the message is keyed on the shield
  being cancelled, not on whether anybody read the result. Running it proved that, and the fix
  changed accordingly.

Known and not acted on: `mirrored_notes` and `item_assignments` grow without ever being pruned,
unlike `webhook_events`. A note row is about sixty bytes and cannot be needed once its delivery
has aged out of the seven day retention window, so pruning it is safe and easy whenever the size
starts to matter. It does not yet.

### What the coverage report had to say

The suite had never been measured, only counted. Running it under coverage put 98% of production
statements on a line that some test executes, which is a better number than expected and not the
point: the useful part was the 43 that nothing reaches, read one at a time to ask whether each was
unreachable by design or untested by accident.

Most were the first. Guard clauses that return early on an empty list, the `raise` that passes a
cancellation along, `setup_hook` and `on_ready`, which need a live gateway. Four were not.

- **A branch in `EventRouter.dispatch` that could not fire and would have done nothing if it had.**
  It answered `ignored` for GitHub's `ping`, which reads as though the ping is handled there. It is
  not: `ping` is absent from `SUPPORTED_EVENTS`, so `will_act_on` refuses it before the route ever
  records a delivery, `register` refuses to take a handler for it, and the very next line in
  `dispatch` returns the same `ignored` for anything unsupported. Three separate reasons it never
  runs, and no behaviour behind it. Proved before removing it, by making the branch raise and
  running the whole suite: 952 passed. Gone, along with the constant it was the only reader of.
  The two endpoint tests that post a ping and expect `ignored` still pass and are where that
  behaviour belongs.
- **The two guards below it, which can fire, and had no tests.** They look like the same
  duplication and are not, because the route and the worker ask at different times. The route
  checks before a delivery is written down; the worker dispatches it minutes or a deploy later.
  Take an action off the supported list, or stop registering a handler, and the deliveries already
  in the queue arrive at exactly these two lines. Raising there would spend sixteen attempts and
  two hours of backoff on work this version of the bot is never going to do. Two tests now, named
  for the deploy rather than for the branch.
- **The race in `/register` had a comment but no test.** Two people running it at the same moment
  both get past the checks for an existing guild and an existing repository, because neither has
  committed, and the database settles it on the unique index. The loser has to hear a refusal
  rather than a driver error with an index name in it. Eight concurrent callers, following the
  shape the `/link` test already uses: exactly one is told it registered, one row and one channel
  mapping exist, and nobody sees anything they cannot act on. Checked it reaches the branch by
  making the branch raise, which failed three of the eight.
- **A fallback in the repository parser, and a comment that oversold it.** It reconstructs the
  owner by splitting `full_name` when the owner block has no usable login, and the comment said
  "some payloads carry only full_name", which is not something any payload read here does. The
  fallback is worth keeping, because guessing the owner is recoverable and dropping the delivery
  is not, but that is the reason and it now says so.

Left alone and worth naming, so the next pass does not go looking: `/pr` and `/issue` each carry
two refusals for states the commands cannot produce, one for a link with no number and one for a
repository with no channel mapped, when the link parsers guarantee the number and `/register`
writes the mapping in the same transaction as the row. They stay because the thing they guard
against is a `SyncsItems` implementation that behaves differently, which is a protocol anything
can satisfy.

### Chasing the rest of the coverage, and what it turned up instead

Thirty statements were left unexecuted after the last pass. Most of the work here was writing the
tests that reach them, which is worth listing briefly and is not the interesting part.

Covered: the GitHub client's unusable pull request body, its non-object JSON body, the client it
builds for itself rather than the one tests inject, and the local clock it falls back to when a
proxy strips GitHub's `date` header. `/set_channel` before `/register`, which had no test at all,
along with what `replaced` says when a type moves channel and when it has never had one. A denial
message for a server that has blanked every role name, which has to stop rather than trail off
after a colon. A comment with no usable id, which cannot be claimed and so cannot be posted once.
A note whose parser refused it, where the step that closes a review request must not run either.
`get_settings`, which wiring calls and nothing else did. Both stores being asked to release an
empty set, asserted as sending no statement rather than as not raising. `run` using the configured
host and port, which were documented settings that nothing read. `setup_hook`, where an installed
command either reaches the tree or silently stops existing in Discord.

Two are worth more than the line they cover.

- **Locking after being overtaken mid-flight.** Only the database half of a sync is ordered. A
  close that was already stale when it started never gets past the database, and there was a test
  for that. A close that is current when it reads and is overtaken while it is talking to Discord
  is a different thing, and locking is where it shows: it is the last step and it is decided from
  a snapshot several calls old, so the reopened issue ends in a thread nobody can post in. Stale
  metadata is corrected by the next delivery; a locked thread is not. Driven by a gateway that
  runs the reopen from inside the Discord call, which is where the window actually is, and checked
  by deleting the guard and watching only this test fail.
- **A shutdown that gives up waiting, and one that fails while it waits.** Both are logged and
  neither had ever been executed, and during shutdown the log is all anybody gets.

The coverage numbers moved from 43 missed to 9. The nine left are a defensive raise for an upsert
that returns nothing, `on_ready`, `if __name__ == "__main__"`, and the three refusals in `/pr` and
`/issue` for states the commands cannot produce.

### Two tests that stopped testing anything under load

The report is what found this, not by what it covered but by what it stopped covering. A run on a
busy machine showed `worker.py` missing the two lines a test named
`test_the_rest_of_the_batch_is_handed_back` exists to reach, while that test passed.

It waited for the worker to lease its batch with `await asyncio.sleep(0.2)`. Leasing is a database
round trip, and on a loaded machine it outlasts any sleep short enough to be worth writing, so the
cancellation landed before the first delivery was handed over. Every assertion in the test is
satisfied by that too: nothing was tried, so nothing used an attempt and nothing was left locked.
It has waited on a condition since, which is what the file's own `_until` helper was written for
and says so in its docstring, three tests above the one that ignored it.

The two reviewer ping tests had the same shape, cancelling on a fixed deadline meant to land
inside the Discord post. Proved by making the thread creation take half a second: the cancellation
lands before the ping is attempted, and both tests carry on passing. They now cancel when the
gateway says the post has begun.

Worth being straight about what that does and does not settle. It fixes a defect those tests
demonstrably had. It is not established that it is the cause of the intermittent failure of
`test_the_owed_ping_goes_out_on_the_retry` seen twice earlier, which fifteen consecutive runs
would not reproduce. The remaining candidate is a real one: the ping claim commits inside a
transaction whose `__aexit__` is a suspension point, so a cancellation delivered after the COMMIT
is sent and before the block returns leaves `logins` unbound, the hand-back unrun and the ping
owed to nobody. That is a window measured in the time a commit takes to acknowledge, and it stays
on the list rather than being called fixed.

### The last six statements, and what branch coverage said afterwards

Five of the six were reachable after all, and the sixth was a flaky test rather than a gap.

The three refusals in `/pr` and `/issue` were written off last time as unreachable, which was
right about the wiring and wrong about the conclusion. `ManualSync` takes its link parser, its
fetch and its `SyncsItems` as constructor arguments, so all three can be driven at the boundary
they exist for: a parser that drops the number, a sync reporting nothing to sync into, and a sync
reporting success with no thread. The last one matters most, because `thread_id` becomes a channel
link in the reply and `None` there is a message pointing at nothing.

`get_or_create` raising when the row it just conflicted with is no longer there is simulated at the
read, because the two statements are inside one method call and nothing can be interleaved between
them from outside. What is under test is what the caller is handed: a `None` flowing on becomes an
AttributeError several frames later with nothing in it naming the item. `on_ready` runs on every
reconnect, when `user` is still None. `python -m shannon.main` goes through the guard at the bottom
of the file, which the console script in pyproject does not, run by path rather than by module name
because the module is already imported and `run_module` warns about that.

The sixth was `registration.py`, which had been covered the round before and was not this time.
Same code, same suite: eight concurrent callers reach the losing path only when the scheduler
interleaves their checks before the first commit, which it does on most runs. Coverage that moves
on its own is the symptom. It is arranged now rather than raced, with one session holding an
uncommitted row so every check comes back empty and the insert stops on the unique index, and with
PostgreSQL itself asked whether a backend is waiting on a lock rather than a sleep guessing at it.
The burst test stays, because one winner and one row across eight callers is a different claim.

Statement coverage reached 100% there, which is a weaker statement than it sounds: every line ran
at least once, not every branch went both ways. Measured properly, four branches out of 446 had
only ever gone one way, and two of them were worth acting on.

- **An optional parameter nothing had ever left out.** `get_by_number` took
  `object_type: ObjectType | None = None` and narrowed the query when it was given. Both callers
  in the services and all four in the tests pass it, so the None path existed for nobody, and the
  only thing the option bought was a way to be handed a pull request when an issue was wanted. It
  is required now and the branch went with it.
- **Both guards in `responses.py`, which had no test of its own.** `reply` picks between Discord's
  two send paths and `defer` does nothing to an interaction already answered. No command reaches
  either, since each defers once and replies once; they are there so a command doing otherwise
  fails visibly rather than by leaving somebody at a spinner.
- **The reload after the rename check.** The GitHub call deliberately sits between two
  transactions, because the sync path refuses to hold one across the network, and that leaves a
  window in which the repository can be unregistered. Reproduced by deleting the row from inside
  the fake's `get_repository`, which is exactly where the window is.

One partial branch is left and is meant to stay. The line-dropping loop in `fit` cannot exit
normally: the function is only entered above two thousand characters, the per-line costs sum to
exactly the length of the message, and the budget is 1998, so it always breaks. Checked by
arithmetic rather than asserted. Restructuring working code so a tool can see that is the wrong
way round, and the number carries the caveat instead.

## MVP 3: status and priority

The first time this bot writes to GitHub. Everything before it read GitHub and wrote Discord, and
the whole design rests on that being one directional, so adding a write needed more care than the
eight commands it delivers would suggest.

### Added

- **Eight commands** (closes the status and priority half of the requirements)
  - `/set_backlog`, `/set_not_reviewed`, `/set_in_review`, `/set_ready_for_merge`, `/set_done`,
    `/set_high_priority`, `/set_med_priority`, `/set_low_priority`.
  - None takes an argument. They act on the thread they are run in, which is the item the person
    running one is already looking at; asking for a link as well would be asking somebody to name
    the thing on their screen. `TrackedItemStore.get_by_number` gained a sibling, `get_by_thread`.
  - Eight separate commands rather than one with a choice, because that is what the requirements
    list and because Discord shows them in the picker as eight things a reviewer can do. They are
    built from two tables in `commands/workflow.py`; the only thing that differs between them is
    the label they set and the sentence they answer with.
  - Reviewers and Project Managers, per the permissions table. Developers are deliberately
    absent: status is the record of what a reviewer decided about somebody's work, and the author
    of that work moving it to ready for merge is the review step going missing.

- **A write path to GitHub** (`add_label`, `remove_label` on the client)
  - Label names are URL encoded, which matters because `priority: high` is a real label style and
    unencoded it changes which path is hit.
  - A 404 on removal is treated as done rather than as failure. Removals are computed from a
    snapshot read a moment earlier, so a label somebody else took off in between leaves the item
    where the caller wanted it, and failing would have them retrying towards a state they are
    already in.

- **`github/labels.py`**, which decides what to take off and what to put on
  - The hard half is removal. Priority has been read from whatever spelling a repository already
    uses since MVP 2, so an item can be carrying `urgent` or `HIGH_PRIORITY`, and a change that
    only writes the new label leaves the old one saying something else. Whatever `parse_priority`
    reads is what comes off.
  - Status is matched only on the exact written spellings, unlike priority. A repository is free
    to have a label called `done` or `review` meaning its own thing, and reading those as
    workflow states would move items through a process nobody asked for.

### The direction that does not exist

Mirroring reads GitHub and writes Discord. Nothing on that path writes back, and it must not: a
label this bot wrote would arrive as a `labeled` delivery, sync, and be written again.

That asymmetry has consequences worth stating rather than discovering. Closing an issue sets the
stored status to DONE and locks the thread, and leaves GitHub unlabelled until somebody runs
`/set_done`. Reopening one clears the stored status and leaves the DONE label behind until
somebody runs another command. Neither is reconcilable without the mirror writing back, so
neither is treated as a defect; the manual route out of both exists and is one command.

The remove-then-add pair generates two deliveries, and GitHub does not guarantee their order.
Processed backwards, the older payload's labels win and the priority reads as it was. The
high water mark cannot break that tie, because both carry the same timestamp. This is the
existing property of mirroring rather than anything new, and it corrects itself on the next
delivery for the item; it is recorded because generating pairs makes it more likely to be met.

### What an adversarial review of it found

Six independent lenses over the new code, each finding handed to a verifier told to refute it.
Eleven survived, ten did not. Three were defects that would have shipped.

- **A closed issue silently reverted any status it was given.** `/set_backlog` on one wrote the
  label to GitHub, stored the status, reported success, and then its own re-render called
  `IssuePolicy.status_for`, which returns DONE for any closed snapshot, and put it straight back.
  GitHub said BACKLOG, the database and the thread said DONE, and nothing reconciled them because
  status is never derived from labels. Three of the six lenses found it separately. Refused now,
  symmetrically with the open issue case that was already there: an issue's status is not this
  service's alone to decide, so both directions of disagreement are refused rather than written.
- **A `/set_done` whose lock failed could never be repaired.** Locking is a separate Discord
  permission, so a server can let the bot open and edit threads and not let it close one. The
  status was already stored as DONE by then, so the retry hit the READY_FOR_MERGE gate and was
  refused for being exactly what the first run had made it. Nothing else locks a pull request's
  thread, since `PullRequestPolicy.locked` returns None. Already being DONE passes the gate now,
  and a repeat is what gets the lock a second go.
- **It locked the thread the command was run in, not the one it wrote to.** A thread deleted
  mid-command has the sync open a replacement, and the lock went to the dead id.

Reverting all three at once fails exactly the three tests written for them and nothing else.

Three comments were wrong, which matters here because they are load-bearing. `labels.py` claimed
to strip `P1`, which `parse_priority` does not read at all, and which had just been corrected in
`priority.py` and missed in the second place. `items.py` said a locked thread rejects edits,
which a test three files away disproves; the ordering it justifies is still right, for a
different reason. The 404 reply said "at that link" for eight commands that take no link.

## MVP 4: project boards

The requirements name three webhook events for this, and none of them can fire. Establishing that
before writing anything was most of the work, and it changed the shape of the rest.

### The events are dead, and the replacements do not reach us

Projects (classic) was sunset on GitHub.com on 23 August 2024, its REST API on 1 April 2025, and
it was removed from Enterprise Server in 3.17; the last release that had it went end of life on
1 July 2026. So `project_card.created` and `project_card.moved` cannot arrive. `project_card.updated`
was never a valid action even while classic existed: the five were `converted`, `created`,
`deleted`, `edited` and `moved`.

All three are still documented, with an availability line and full action lists, which is a
leftover in the published schema rather than a promise. A bot subscribed to them receives nothing,
for ever, with no error, and that is exactly what would have been built.

The replacements are `projects_v2`, `projects_v2_item` and `projects_v2_status_update`, and they
are organisation scope only. A repository webhook receives none of them. A project owned by a user
account emits none of them at all, and `Canon-Regularis` is a user account, so there is no event to
subscribe to and no configuration that creates one.

### So the board is polled

GitHub shipped a REST API for Projects v2 in September 2025, and unlike the webhooks it covers
user-owned projects. `ProjectPoller` reads the board on a timer beside the delivery worker. The
cost is latency, bounded by the interval; the saving is that a personal board and an organisation
one work on one code path with no second webhook to install.

The board answers with every card every time, so the work is deciding which of them moved, and
most of what is tested here is what does not happen: an unchanged board is not mirrored again, one
card per thread however often it is read, and nothing at all when no board is configured.

### Added

- **A third object type.** `TicketSnapshot` satisfies `TrackedSnapshot` with nothing added, which
  is the open/closed property this codebase claimed months ago finally cashing out. `TicketPolicy`
  renders the three-line block the requirements give a ticket: name, link, status, and none of the
  eight other rows, because a draft card has none of them and empty fields read as data missing
  rather than data absent.
- **`domain/board.py`**, mapping a column name to a status. Deliberately more forgiving than the
  label matcher and for the opposite reason: a label namespace is shared with whatever else a
  repository labels things, so `done` there may mean anything, while a Status column is a small set
  somebody chose to describe this workflow. GitHub's own default template, `Todo` / `In Progress` /
  `Done`, is understood, so the board most people point this at first needs no configuration.
- **Cards that wrap an issue or a pull request** move the thread that item already has, through
  `ItemWorkflow.set_status`, which is the path a person takes with `/set_in_review`. Never a second
  thread. Decided by comparing statuses rather than timestamps, because a card and an issue keep
  two different clocks and a card edited for any other reason must not re-assert a status.
- **`/set_channel` offers tickets**, and they have no fallback channel unlike issues. An issue with
  nowhere to go is a mistake; draft cards appearing uninvited in the pull request channel would be
  a surprise.
- **Thread naming moved onto `SyncPolicy`.** It was a module function producing `#7 Title`, and a
  ticket has no number, so it would have read `#0 Title`.

No migration. `github_object_type` is a varchar rather than a native enum precisely so a later
object type costs nothing, and `test_migrations` diffing the applied schema against the models
passes untouched.

### Verified rather than assumed

The token available could not read projects, so the response shapes came from GitHub's published
OpenAPI description rather than from prose or from a live call. All eight user-project routes exist
as built and `content_type` is exactly `Issue | PullRequest | DraftIssue`.

It caught a real defect. The items endpoint has no `page` parameter: pagination is by a cursor in
the Link header. Asking for page two by number is not an error, it is silently the first page
again, so a board of more than a hundred cards would have been read fifty times over and every card
mirrored fifty times. Paging follows the Link header now, bounded, because the cursor is opaque and
a header pointing at itself is indistinguishable from a real next page.

One trap survives being documented only by example: for a single-select field, `value.name` is an
object rather than a string, unlike every other name in that API. Read as a string it returns None
and every card looks statusless, with no error anywhere. Both shapes are accepted.

### What an adversarial review of it found

Four lenses, each finding handed to a verifier told to refute it. Nine survived, five did not.

- **A draft could be recorded and then never shown, indefinitely.** The sync path commits the row,
  timestamp included, before it calls Discord. A thread creation that failed left a row that looked
  current with nothing to show for it, and a filter comparing only timestamps skipped it on every
  later poll. Nothing else rescues a draft: an issue gets another webhook, a draft has only this.
  The filter reads the thread as well as the timestamp now.
- **Every workflow command raised `KeyError` in a ticket thread.** The thread lookup does not filter
  by kind and the workflow registers only pull requests and issues, so `/set_in_review` in a ticket
  thread hit a bare dict lookup, reaching the user as "Something went wrong here" and the log as a
  traceback. It refuses in words now, and says the true thing: a draft has no labels, move its card.
- **One bad card took the rest of the poll with it.** The wrapped half isolated each card and the
  draft half did not, and drafts run first, so a single card Discord refused skipped every later
  draft and the entire wrapped half, none of which had anything wrong with them.
- **A `content_type` that was not a string ended the poll.** A dict lookup on an unhashable key
  raises rather than answering None.
- `_VERDICTS` was defined twice in `formatting.py`, which predates all of this.

Reverting the three behavioural fixes fails exactly the three tests written for them.

### Still open

Nothing here has run against a real board. The parser is checked against GitHub's schema and every
field is guarded, but a shape that differs would show as an unread card rather than an error, which
is the failure mode worth knowing about before the first live poll.

## MVP 5: re-planning and refactorisation

Two halves, and the second one is a list rather than a change: a survey of where the built thing
and the written thing have drifted apart, which is a set of decisions rather than defects.

### The board stopped overruling commands

The worst thing this pass found, and it was a week old rather than a year. `_move_tracked`
compared a card's column against the item's stored status and acted whenever the two differed.
That answers the wrong question. A reviewer running `/set_ready_for_merge` on a pull request whose
card still sat in `In Progress` had the decision reverted by the next poll, silently, inside the
interval, because from where the poller stood a standing disagreement and a fresh move look
identical.

It compares against the column it last saw now, which is what migration `0009` is for. Three
decisions inside that, none of them forced:

- **First sight fills a blank and never overwrites a decision.** With no remembered column, the
  board applies its status only if the item is still NOT_REVIEWED, which is nobody's opinion. If
  somebody has already set one, the column is recorded and left alone. That is what stops a board
  added after the fact from undoing work that predates it.
- **A refused move is still recorded.** A closed issue refuses anything but DONE, but the card did
  move; remembering only the moves that worked would repeat the same complaint every poll for the
  life of the process.
- **Columns compare trimmed and case-folded**, so renaming `Done` to `done` is not a move.

### Refactorisation

- **The layering rule was broken, by MVP 4.** `github/projects.py` imported `BoardItem` from
  `services/projects.py`, and `github/` sits below `services/`. That is the one architectural
  invariant this codebase has. `BoardItem` lives in the adapter now and the service imports
  downward; the whole package was checked for other upward imports and has none.
- **`GitHubClient` did not declare two methods the wiring depends on.** The container hands that
  object to the board reader, which calls `get_json` and `get_pages`. A stand-in satisfying the
  protocol built a container that died on the first poll instead of failing at the seam, which is
  exactly what the conformance table exists to prevent, and it caught the gap the moment the
  protocol grew.
- **Two timestamp parsers in one package.** `mapping.parse_timestamp` already existed; MVP 4 wrote
  a second one doing the same thing.
- **A GitHub outage was logged as a board disagreement.** Every GitHub and Discord failure is a
  ShannonError, so catching that alone reported an outage as forty cards with unsuitable columns.
- **A bad `fields` response was cached for ever.** One wrong answer left every card on the board
  with no column until the process restarted, and nothing distinguishes that from a board whose
  Status field was genuinely renamed. Failures are not remembered now.

### The drift, as a list of decisions

Recorded rather than acted on, because each is a choice about what the product is.

Three of those were acted on rather than recorded, and the section below says what happened to
them. Issue blocks still omit the `Reviewers:` line the output format lists for every item, which
was left alone deliberately: GitHub issues have no reviewers and a row that always reads `None` is
noise rather than information, so `requirements.md` records the difference instead.

Where the specification is stale: `item_assignments.discord_user_id` was dropped in `0008` as a
copy of `user_links`; `webhook_events` became a leased queue and grew five columns; `tracked_items`
grew four; `item_assignments` grew the two claim stamps that stop a double ping; `mirrored_notes`
and `user_links` are whole tables the list never mentions, each added because a specific bug
demanded it; and `Priority.UNSET` is a fourth value because an item with no priority label has to
be something.

Genuinely either way: a guild administrator passes every command gate, where the spec grants Admin
only `/register`. Roles are never pinged, because `allowed_mentions` sets `roles=False` so that
GitHub-authored text cannot reach `@everyone`, and turning bot-authored role pings on means a
narrower rule rather than removing that one. The metadata block carries a `State:` line the output
format does not list. And a status label somebody sets by hand on GitHub is never read back, so the
duplex claim in the Goal holds for board columns and for commands but not for a label typed
directly.


### The three that were acted on

- **A review asked of a GitHub team reached nobody.** Only `requested_reviewers` was read, so a
  pull request whose only reviewer was a team stored nothing and said nothing. Teams are read now
  and shown in the reviewers line, and they are deliberately not given assignment rows. A team
  cannot be mentioned, because `/link` binds a GitHub login to a Discord account and a team has
  none, so a ping could only ever be its name in plain text. Against that, a team row is
  indistinguishable from a person's, and `fulfilled_at` is set by the login of whoever submitted
  the review, which is never a team, so the stamp that stops a retried delivery pinging somebody
  about work they already reviewed would be inert. A review of this found the worse half: with
  the row never fulfilled, a re-request of that team pings nobody at all, because `replace` sees
  an unchanged set, `reopen_if_newer` has no stamp to clear and `claim_notifications` skips a row
  already notified. Closing a team request properly needs a membership lookup no payload carries.
- **`ActorRole.PROJECT_MANAGER` is gone.** It is a Discord permission tier, and no fact about a
  pull request or an issue produces one, so nothing ever wrote it and nothing could have. No
  migration: `role_type` is a plain varchar with no constraint, which is what `varchar_enum`
  exists to explain, and no row can hold a value nothing ever wrote. An earlier draft of this
  section said there was a CHECK constraint. There is not.
- **The specification now matches the schema**, table by table, with the reason beside each
  column the original list did not have.

### And two the same review found in the fix before it

- **The board move was recorded before it was attempted.** Written to justify the refusal case,
  where a closed issue will never accept BACKLOG and re-complaining every poll is pointless, and
  wrongly applied to every failure. A rate limit or a 500 is not a refusal, and marking the move
  as seen meant no later poll looked at that card again, because nothing else ever rederives a
  status from a board. The column is written on the terminal paths and not on the retryable one.
- **Null meant two things.** Never seen and seen-with-no-column both stored null, so clearing a
  card's Status put it back to never seen, re-armed the first-look guard and dropped the next
  real move. The empty string means seen with no column.

### One test that was measuring the weather

`test_finding_an_item_by_number_uses_an_index` ran EXPLAIN against a table holding one row. On one
row a sequential scan really is cheaper and PostgreSQL is right to choose it, so the assertion held
only while the table had no statistics and flipped whenever autovacuum reached it first. It fills
the table and analyses it now, which is the size the test was always about.

That makes three tests this stage that passed for reasons unconnected to what they claimed, and a
fourth that waited on a fixed sleep for two poll cycles and stopped testing anything under load.
The pattern is worth naming: a green suite says the code does what the tests do, not what they say.

### Teams, and the two defects that came with them

A review can be asked of a GitHub team, and until now that request reached nobody. Only
`requested_reviewers` was read; `requested_teams` was not. Fixing it needed two halves at once,
because a team recorded without somewhere to look up who it is on Discord's side is a row nothing
can ever close.

So `team_links` maps a team to a Discord role, `/link_team` writes it, and `ActorRole.REVIEWER_TEAM`
keeps teams apart from people. Apart because the two are told in different words off different
tables, and closed by different rules. Role mentions had to be allowed for any of it to deliver,
which is safe for a reason worth stating: every scrap of GitHub-authored text goes through
`defuse_mentions`, which breaks `<@&123>` as well as `<@123>`, so the only live mentions in any
message are the ones this bot built. `everyone` stays off, because nothing it builds is addressed
to everyone.

A review of the result found two things, both mine, both an hour old.

- **A team was re-pinged once per review round.** Closing every team's request when anybody
  submitted a review left rows stamped that GitHub had never dismissed, and `reopen_if_newer`
  cannot tell that stamp from a genuine re-request. The next ordinary event with a later timestamp,
  a label or an edit, cleared both stamps and pinged the role again for a request nobody had
  answered or re-made. Reproduced before fixing: one ping became three.

  The reasoning behind that code was wrong in both directions. The double ping it was written to
  prevent cannot happen, because `replace` leaves an existing row alone and a row that has been
  pinged keeps its `notified_at`. And the comment claiming the cost was "a team goes untold about a
  re-request" had it backwards: it was told again, repeatedly. A team's request is closed by GitHub
  dropping it from `requested_teams`, which deletes the row, and that is the whole mechanism needed.

- **A team could be impersonated.** Putting teams into the assignments mapping meant team slugs
  were passed to the user link store, which maps GitHub logins to accounts. `/link` has no gate and
  asks GitHub nothing when somebody claims a name for themselves, so any member could `/link
  security` and then appear as, and be pinged as, the `security` team on every pull request in the
  server. Slugs and logins are separate namespaces on GitHub and collisions are ordinary; this one
  is claimable on purpose. Both halves needed closing, since a team colliding with somebody
  genuinely on the item would still have rendered as them.

### What a green suite did not say

Six tests this stage passed for reasons unconnected to their names. One asserted teams were dropped
and so pinned the gap in place. One reached its branch by a path that never touched the code it was
named for. Two waited on a fixed sleep and stopped testing anything under load. One read the query
planner's mood on a table holding one row. One cancelled a loop where the clause it tested could
not fire.

Every one of them was green, and the suite was at a hundred per cent of statements and branches
throughout. What actually found things was reading downstream of a change, a coverage number
refusing to move after it should have, and reviewers told to refute rather than confirm.


### Linking is a project manager's job now

`/link` gated the case where somebody linked an account on another member's behalf and left the
self-claim ungated, on the reasoning that your own account is yours to claim. Nothing checked that
it was. GitHub is never asked whether the login belongs to the person typing it, so anybody in the
server could run `/link torvalds` on themselves and from then on receive every mention meant for
that login: the reviewer ping, the assignee ping, and the mention in every metadata block.

It is the same route by which somebody could have become a review team before teams were given a
table of their own, and it is closed the same way, which is that a person who speaks for the server
does the pointing. The `member` argument still defaults to the caller, so a project manager linking
themselves types no more than before. What changed is who may call it at all.

Real verification would mean asking GitHub whether the account is theirs, which means OAuth and a
consent screen, and that is a different piece of work from a slash command.

### Running it with no Discord, and what fell out

Nothing here came from the test suite. The app was assembled through `build_app`, given its real
lifespan, a real database and a real signed delivery, and run with no Discord token, which is the
documented no-bot mode. A pull request arrived, the worker picked it up, and every attempt failed
with `AttributeError: '_MissingSentinel' object has no attribute 'is_set'`.

That is discord.py's internals talking. It is not an `HTTPException`, so it went straight past the
translation that turns Discord's errors into this project's, and the worker retried an unreadable
internal error sixteen times and wrote it into `last_error` for somebody to puzzle over. Every
operation on a thread resolves a channel first, so the guard goes there: a client that has never
connected refuses with a gateway error, which is retryable, because a bot that has dropped usually
comes back and the delivery should be waiting when it does.

### Six ways one card could stop the board, and one request that told nobody

A round of adversarial review, with each finding handed to a second reader told to refute it. Five
of the six survived that and one was narrowed; the sixth was found by reading the same code again
afterwards. Together they are two shapes, and both are worth naming because neither shows up as a
failure anybody would notice.

**A poll that stops half way stops for good.** `run_forever` catches everything and waits out the
interval, which is right, but it means a card that raises where nobody wrote a branch ends
`run_once` in the middle and takes the cards behind it and the whole wrapped half with it. Nothing
is recorded, so the next poll reads the same board and stops at the same card. The board mirror is
off for the life of the process and the only sign of it is one traceback a minute.

- A draft card's Title is free text with no cap on GitHub's side, unlike an issue's, which GitHub
  holds to 256 characters. Anything past 512 does not fit `tracked_items.title` and raises out of
  the flush as a `DBAPIError`, which is not a `ShannonError` and so missed the per-card handling.
  Board text is now cut to what the row holds before it is written.
- The same for a Status column past 128 characters. The board's Status field is matched by name and
  never by type, so what arrives can be free text somebody pasted in. Cut at the same place, before
  the comparison rather than at the write, so what is compared next poll is what was stored.
- Both halves of the poll now handle a card that fails in a way nobody wrote a branch for: logged
  whole, with the traceback, and the board carries on to the next card.

**Progress written down for a step that failed.** Nothing raises and nothing is logged as an error.
A run gets half way, records the half it did, and the next poll reads that half as the whole.

- A card dragged to Done whose thread Discord then refused to lock. The status is stored in the
  middle of `set_status` and the lock is last, so the retry the poller deliberately left open was
  already dead: the next poll saw the status matching and wrote the move off as seen. A finished
  pull request kept an open thread for ever, and nothing else locks one. The column, not the
  status, is the record of a move having been carried through, so a card whose column says
  otherwise now goes round again.
- A draft whose thread edit was refused. The row is committed before the Discord call, on purpose,
  and a webhook delivery that fails after it is retried by the worker. A card has no worker: it
  comes back only when GitHub's timestamp beats the stored one, and the failed sync had just made
  those equal. The mark is now put back when the mirror does not happen.
- A card the sync refused was counted as mirrored, because only a raise counted as failure. A
  repository with no channel mapped for tickets has every poll report the whole board mirrored,
  for ever, directly under the warning saying the opposite.

**A team asked for a review twice is told once.** GitHub drops a team from `requested_teams` the
moment any member submits a review, and sends no `pull_request` event saying so. The row survives
with its ping stamped, so the next ask arrives with the list exactly as it was, `replace` leaves the
row alone, and nobody is told. There is no escape either: `synchronize` is not a handled action, so
a round of review, fixes and re-request produces no delivery that would have deleted the row.

The fix is the field GitHub sends for exactly this. A `review_requested` event names the party at
the top level, and GitHub only sends one for a party that was not already requested, so being named
there means the last request is over and this is a new one. Acted on once: the payload has to be
newer than the item was before it, or a replayed delivery pings everybody a second time. Both sides
of that comparison are GitHub's clock, never ours. A person has the same hole whenever the review
event never reaches us, and it closes the same way.

### A second hunt, and the four lenses the first one never finished

The first sweep lost seven of its thirteen readers to a session limit, including the ones for
races, hostile input, data integrity and vacuous tests. This ran those again, plus a reader whose
only job was to attack the fixes above, and each finding went to a second reader told to refute it.
Eight survived. Four of them are named below; the rest were already closed by the work above, and
one of those was found twice, independently, which is the first time anything here has been.

**A request has an age, and nothing recorded it.** Two of the eight were the same missing fact.
A review request row carried when we pinged and when a review closed it, both on our clock, and
nothing said when the request itself was made. So:

- A `pull_request_review` delivery runs the ledger before its Discord post and again on every
  retry, and it has sixteen attempts across two hours to be retried in. A re-request made inside
  that window was closed a second time by the next attempt, with the review's own timestamp. The
  stamp then read as an answered request, and the next label or edit reopened it and pinged for an
  ask nobody had made.
- Telling a re-request from the same delivery arriving twice was done by comparing the payload
  against the tracked item's high-water mark, which is a question about the item and not about the
  row. GitHub sends one delivery per party asked and gives them the same second, so one review
  request naming two people raised the mark on the first and the second read it as a replay: the
  party named second was never pinged.

`item_assignments.requested_at` is the fact both were guessing at, on GitHub's clock so that both
sides of every comparison are on the same one. Set when the row is written and moved forward when
the request is made again; a review will not close a request younger than itself, and a re-request
older than the row is a replay. The item-level proxy is gone.

**A finished pull request could never be discussed again.** `/set_done` locks the thread, and
nothing anywhere unlocked one: `PullRequestPolicy.locked` returns None on every sync, so the only
lock a pull request ever gets is that one and no webhook was ever going to lift it. Moving it back
out of DONE is allowed, and `/set_in_review` wrote the label, moved the stored status, reported
success, and left the thread shut. It gives the thread back now, and only when DONE is on one side
of the move or the other, so an ordinary status change still costs no call to Discord.

**A sync decided it was current before anything was locked.** The staleness check
reads the item's mark and acts on it several statements later, under read committed, which gives no
snapshot stability inside a transaction. Two syncs of one item overlap by design, whether `/pr`
beside the worker, several events for one item at once, or a second replica, so both read the mark
before either
commits, both answer "not superseded", and neither takes the stale exit. The one carrying the older
payload then writes its whole snapshot over the newer one, down to deleting a reviewer's row with
its `notified_at` and putting back somebody the newer payload had removed, who is then pinged again.

The read holds the row now, so the second one in sees what the first wrote and decides against
that instead. A new item has no row to lock yet, which is what `get_or_create`'s own conflict
handling was already for.

Worth recording how nearly this went the other way. The same three lines were written once before
and taken straight back out: the run carrying them was three times slower than the run without and
two tests failed in a fixture, so it read as an exclusive lock on the busiest row of the sync path
starving the suite. That was wrong. The failures were `asyncpg.connect` giving up after sixty
seconds, which is connection pressure and not lock contention, and the pressure was several test
runs and eighteen review agents sharing one PostgreSQL. Measured again on a quiet machine the
locked suite is 8:13 against 11:02 for the unlocked one, which is to say the difference was never
there at all. The lesson is the cheaper one: a measurement taken on a busy machine is not a
measurement, and a fix should not be abandoned on the strength of one.

**Two more from the same session, found by hand.** A board read hands the same card back more than
once whenever a cursor pages through a list somebody is editing, and the draft half mirrored every
copy, because the stored state is read once for the whole board and never written to. And a spent
GitHub rate limit was polled straight through: the client already works out when the window
reopens, and nothing read it, so the poller went back every interval for the whole window, worse
than wasted, since GitHub lengthens a secondary limit for requests made during one.

### What the tests were not saying

Two tests were found passing for reasons unconnected to their names, both by mutating the code
they were named for and watching them stay green.

`test_a_review_with_no_author_is_left_alone` ran against a repository nobody had registered, so it
returned two guards before the one it was named for and went on passing with that guard deleted.
It asserted nothing at all, as did the two beside it, so any of them could have closed a request
and stayed green. All three have a real open request in front of them now and say what happened to
it.

`test_github_is_written_before_discord` failed every GitHub call, including the read that comes
first, at which point nothing has been attempted and the order it is named for is never
exercised. It passed with the label write moved after the re-render. The refusal is on the write
now, and it checks that Discord was left alone.

A third was accurate and is not a defect: nothing pins the sync path's team-slug filter, because
the renderer names teams plainly and never looks one up in the account map, so deleting the filter
changes no behaviour today. It is defence in depth and its comment now says so rather than
claiming to be the thing standing in the way.

### Ruled out

Recorded because they were checked properly and are worth not checking again. Every migration
round-trips: applied to head, taken down to base and back up, `alembic check` finds nothing and the
schema is byte-identical to a fresh one. The four webhook parsers were given 7368 mutated bodies,
every field of every real payload replaced with each of twenty-three hostile values and separately
deleted, and raised nothing. The endpoint's own limits hold over real sockets, as does redirect
following, Link-header pagination and the rate-limit classification. `.env.example` was missing
four settings, one of them the number that turns the board mirror on, so a guard now compares it
against the settings object.

## A third hunt, over ground the first two never touched

Six lenses again, none of them repeats: the delivery queue and the worker's lifecycle, process
startup and shutdown, the pure domain layer attacked with invariants rather than examples, what
actually reaches a Discord message, at-least-once end to end, and whether what is documented is
what happens. Ten findings survived a reader told to refute them, and none was refuted. Nine are
fixed below. The tenth is recorded at the end.

### The documented install did not work

`docker compose up` could not bring the stack up after the README's own first step. Compose reads
`./.env` for interpolation, the README says to begin with `cp .env.example .env`, and that file
carries the host's `localhost:5433` database URL. So `${SHANNON_DATABASE_URL:-...@db:5432/...}`
never reached its default and handed both containers their own loopback, where nothing is
listening: `migrate` died on connection refused and `app` waited on it for ever. The one setting
whose value inside the network is not the value outside it is now written out rather than
interpolated, and `docker compose config` was read back both with and without a `.env` to prove it.

### The image reported healthy at the moment the service reported it was not

The Dockerfile's HEALTHCHECK was a bare TCP connect, under a comment claiming the service exposes
no health route. It does, it answers 503 when the database is unreachable or the worker has died,
and that is the whole reason it exists. A socket that opens proves only that uvicorn is listening,
and uvicorn goes on listening with a dead worker behind it and a queue that only grows. CI's image
job polls container health, so this was the check that actually decided. It calls `/health` now,
through urllib rather than curl, which the slim base image does not carry.

### Three ways the process could stop doing its job and say nothing

- **The worker waited for Discord for ever.** discord.py reconnects by design, so an outage,
  blocked egress or a handshake that never completes leaves `start()` running and the connection
  never made. The worker sat in the pre-loop wait for the life of the process: nothing leased,
  nothing pruned. It goes ahead without Discord after five minutes now, and every delivery then
  fails with a retryable gateway error that says why. A queue draining slowly with a visible reason
  beats one that never moves.
- **`/health` called that healthy.** Both it and the gateway check answered on the bot's task not
  having finished, which is true of a client retrying for ever. It asks the client whether it has
  actually arrived.
- **The startup database check had no deadline.** asyncpg's sixty seconds bound the handshake, not
  the query, so a server that accepts the connection and then goes quiet left the lifespan blocked
  before `yield`. Uvicorn opens no listening socket until startup returns and reads a signal only
  afterwards, so the process served nothing, answered no health check, and could not be told to
  stop. Fifteen seconds now, then it fails with something an operator can act on.

### A batch nobody handed back

Everything a handler can raise is dealt with inside `_handle`. What escapes it is the write that
records the outcome. That the delivery itself comes round again when that write fails is the
documented contract, `mirrored_notes` exists to make it safe, and a test pins it, so that half is
left exactly as it was. What was not intended is the rest of the batch going with it: up to nine
deliveries nothing had touched sat marked PROCESSING under a live lease, invisible to this worker
and to any other, until it lapsed a quarter of an hour later.

They are handed back now, from the failing delivery onward, and the error still comes out of
`run_once` so that deciding whether to carry on stays with `run_forever`. Because `release` only
moves rows still marked PROCESSING, a write that did commit is left alone and one that did not
comes back for another go. Worth recording that the first attempt at this swallowed the error as
well, and the test that already pinned the contract is what caught it.

### The priority commands never converged on a repository that spells its labels in lowercase

`status_change` compares case-folded and `priority_change` did not, in the same module. GitHub's own
stock labels are lowercase, and it matches a label name without regard to case, so an item carrying
`high` had that label read as stale purely for its case: it came off, `HIGH` went on, and the add
re-attached the very label just removed. The item still read `high`, the next run of the command did
the same two writes again, and every one of them answered "is now HIGH priority" for an item that
had been HIGH all along. Beside it, the re-render appended the label being added even when the item
already carried it, so a pull request holding `HIGH` and `urgent` rendered its tag twice, and which
of the two happened depended on the order GitHub returned the labels in.

### And the README described a schema it no longer had

Seven tables where there are eight, `team_links` missing although the same README documents the
command that needs it, and a revision range stopping at `0007` with eleven on disk. Three of the
README's claims are restatements of things the tree already decides, so three tests now compare
them against the tree: every table on the metadata, every setting on the object, and the revision
range against the files. The changelog's own claim that enums carry a `CHECK` constraint is
corrected too. They never have: `create_constraint` has defaulted to False since SQLAlchemy 1.4,
`varchar_enum` says so, the README says so, and a later stage had already removed an enum value on
the strength of the opposite being true.

### Left alone, on purpose

`fit` drops from the first line that will not fit rather than skipping it and keeping the shorter
lines below, so a very long field costs the fields under it. That is what "trim on a line boundary"
means, a test pins the prefix, and the alternative leaves a hole in the middle of a block whose
trailing marker says it was cut at the end. Recorded rather than changed, because it is a judgement
somebody already made and wrote down.

## A fourth look: the race closed, and a mutation campaign

The six readers planned for this round all died to a rate limit before they read anything, so this
was done by hand and by machine instead.

### The overlapping-sync race is fixed

The three lines that close it were written two sessions ago, measured, and taken straight back out
because the run carrying them was three times slower than the run without and two tests failed in
a fixture. That reading was wrong. The failures were `asyncpg.connect` giving up after sixty
seconds, which is connection pressure rather than lock contention, and the pressure was several
test runs and eighteen review agents sharing one PostgreSQL. Measured again with the machine to
itself, the locked suite is 8:13 against 11:02 for the unlocked one.

So the read that decides whether a delivery is stale now holds the row for the rest of its
transaction. Two syncs of one item overlap by design, and without it both read the mark before
either commits, both answer "not superseded", and the one carrying the older payload writes its
whole snapshot over the newer one: title, state and priority, and the reviewers, whose rows
`replace` deletes and reinserts with `notified_at` cleared, so somebody the newer payload had
removed goes back on the item and is pinged for it again.

### Seven guards nothing was checking

A generated mutation campaign over ten modules: every comparison and boolean operator flipped one
at a time, eighty-five mutants, each run against the tests that name its module. Sixty-one died.
Of the twenty-four that lived, seven were real gaps and are now closed.

- **`LabelChange.nothing_to_do`** is `not remove and not add`, and turning that `and` into an `or`
  changed nothing any test could see. Every assertion about the property said it was true and none
  said it was false. What the wrong version costs: an item already stored at BACKLOG, wearing a
  stray second status label, asked for BACKLOG again returns early and never strips the stray one.
  The test that looks like it covers this does not, because its stored status differs from the one
  being set, so the guard's second half is false and the early return never fires either way.
- **Six guards in `mapping.py`**, all the same shape. `if not isinstance(x, str) or not x` turned
  into an `and` accepts a field that is present and empty, or present and the wrong type, and no
  test noticed for the login, the team slug, the repository name, a label name, or either of the
  two link fallbacks. There was no test file for that module at all: the parsers were covered
  against payloads missing a key or shaped wrongly at the top, never against a key that is there
  and useless. What the wrong version costs is not an exception but an `Actor` whose login is the
  empty string reaching the assignment store and the renderer, and a blank tag in a thread.

### And seventeen that were not

Recorded because a mutation report is only worth reading if it says which survivors do not matter.
Three are boundary flips on timestamp comparisons, `<` against `<=`, which differ only when two
times are equal to the microsecond. Two are inside a logging branch that runs while the process is
shutting down. The other twelve were an artefact of the campaign itself: it ran each module against
the tests that name it, and `get_by_number` is reached by the note path rather than by the sync
files, so a dozen WHERE clauses looked unpinned when they were not. Re-run against the whole
integration tier, they die. The lesson is about the method rather than the code: a surviving mutant
is a question, not an answer, and the first thing to ask is whether the tests that should have
killed it were even running.

## A fifth look, to a product standard

Six lenses again: authorisation boundaries, what happens to everything already mirrored when the
setup changes underneath it, the issue path, behaviour at volume, the protocol seams, and what a
person actually sees when it goes wrong. The readers ran out of quota partway, so two findings got
a full adversarial verification and the rest are recorded below as leads rather than as facts.

### A comment on a deleted thread was the comment that got lost

Somebody deletes an item's thread in Discord. The next comment or review arrives, cannot post,
lets go of the dead pointer and asks to be tried again. Nothing rebuilds the thread: only a sync
can, because only a sync has the channel and the metadata, and a comment is not an item event. So
the retry found the pointer still null, raised the same thing again, and sixteen attempts and two
hours later the note was dropped. Every comment and review left in the meantime went the same way,
until some unrelated `pull_request` or `issues` action happened to arrive and rebuild the thread as
a side effect. On a quiet item that is never.

The note mirror now asks for the rebuild it was already assuming would happen, and its own retry
posts the note into the new thread. That is the only call to GitHub anywhere on the note path and
it fires when a thread has actually gone rather than on every comment, which is worth stating
because it is a dependency the path did not have before. It is optional at the wiring rather than
baked in, so a mirror with nothing passed behaves exactly as it did.

### A delivery from the past renamed the repository back

An item whose thread has gone is deliberately not turned away as stale, because the thread has to
be rebuilt however old the delivery is. That one path reached `_write` with a superseded payload,
and the rename was being followed above the guard that refuses to believe the rest of it: a
repository renamed or transferred on GitHub had its stored name and URL rolled back to whatever it
was called before. Nothing rewrites that row afterwards, so it stayed wrong until the next current
delivery, which for a quiet repository may never come.

The fix moves the rename below the guard. Worth recording why it was missed: an earlier round fixed
exactly this defect on the ordinary stale path by returning early in `_resolve`, and wrote in this
file that the rename "is now followed only for a delivery that is actually current". That was true
of the case it tested and not of the rebuild bypass, and the test that pins it never reaches
`_write` at all, so the second half stayed broken with a passing test and a changelog entry both
saying otherwise.

### What a user is told when GitHub says no

Three different answers were arriving as "GitHub could not be reached", which was wrong for two of
them and useless for all three. GitHub had answered.

A spent rate limit now says when to come back, using the reset moment the client works out from
GitHub's own headers and which nothing had ever read. A refused token now says the bot's access was
refused and an admin needs to look at it, because nobody running a command can fix that one, and it
no longer echoes an API path at somebody sitting in Discord. Only a genuine outage still says the
service could not be reached.

Two of the three tests that pinned the old wording were testing something else and had hard-coded
it in passing. The third used the rate limit as its example of falling through to the general case,
so it was pinning the defect; it now uses the error that really does mean unreachable.

### The leads, chased

The readers that would have verified these ran out of quota, so they were recorded as leads and
then checked by hand. Three were real and are fixed, one is real and is not, and three were
decisions somebody had already made and written down.

**A board nobody had finished setting up wrote one warning per card per poll.** Tickets have no
channel fallback, so a board configured before `/set_channel` mirrors nothing, and every card was
asked and refused separately: a session, two queries and an identical warning each, once a minute,
for as long as nobody noticed. Nothing about any one card decides that, so the first refusal now
ends the pass and says once what is wrong and what to do about it.

**The poller asked the database once per card.** The draft half of a poll already reads the whole
board's state in one query; the wrapped half asked per card, opening a session each time, for every
card whether or not it had moved. A board is read whole every time, so the number of questions
should follow the number of boards and not the number of cards. It reads once now, the way the
half beside it always did.

**A closed issue can be reopened by a delivery from the same second.** Reproduced: an issue closed
and locked, then the retry of an event stamped the same second lands and takes it back to
NOT_REVIEWED with the thread unlocked, and nothing corrects it until the next event. This one is
recorded rather than fixed, because the obvious fix is wrong. `is_superseded` counts equal
timestamps as current on purpose, and the reason holds: GitHub stamps to the second, two changes
often share one, and treating equal as stale would drop the second of a pair arriving in order,
which is usually the one carrying the newer state. Telling those two apart needs an ordering the
timestamps do not carry, and the queue does have one in its own row ids. That is a schema and a
signature change rather than a comparison, and it wants deciding rather than bolting on.

**Three were already decided.** `tracked_items.discord_thread_id` carries no index, and the store
says why: one row per thread and a handful of commands a day is not an index worth maintaining on
the sync path. The poller reads whichever repository registered first, and `only_one` says why:
one per guild is a constraint, and the process serves one guild. Neither is a defect. The third is
a genuine limitation rather than a bug: there is no way to unregister a guild, so a server that
wants to move to a different repository has no supported route, and the board is addressed by an
owner taken from the repository name, so a transfer to another account silently repoints it. Both
are product decisions and are recorded here as such rather than changed.

## A sixth look, before it runs on a real server

The last few rounds read the code. This one asked a different question: what breaks the first time
somebody actually starts it against Discord and GitHub, which is a question no test with a fake can
answer, because a fake accepts whatever it is handed. Six lenses covered the Discord seam, the
GitHub seam, the protocol boundaries, running more than one copy, recovery, and configuration at
its extremes. Everything below either failed for real or was reproduced against a live database.

### The invite link was missing a permission, and the failure looked like nothing

The metadata block at the top of a thread is one message, written when the thread opens and
rewritten on every event after. Rewriting it means reading it first, and Discord counts reading a
message the bot wrote itself as reading history. `Read Message History` was not in the permission
list in the README.

The failure mode is the bad kind. A refusal becomes `DiscordPermissionError`, which is permanent,
so the delivery is given up on the first attempt rather than retried. The thread appears once,
correctly, and then never changes again, and nothing in Discord says why. Documented now, along
with what goes wrong when it is missing.

### Slash commands take up to an hour to appear, and the log said they were done

Commands are registered globally rather than per guild, which is right: a global registration
survives being added to a second server and needs no guild id at startup. Discord serves global
commands from a cache that can take an hour to catch up, so on a fresh application the commands do
not exist for anyone yet. The log said it had synced them, which reads as ready.

The log now says what actually happened and the README sets the expectation. No behaviour changed;
the wrong expectation was the whole defect.

### Nothing anywhere checked what Discord will accept

Discord validates every command name and description at sync time, and a violation does not fail
gracefully: the sync raises, `setup_hook` raises, and the process ends without connecting. Every
test of a command drives its callback directly, and the fake gateway syncs nothing, so a rename to
an invalid name would have passed the whole suite and then refused to boot.

`tests/unit/test_the_commands_discord_will_accept.py` builds the commands the container really
installs and holds them to Discord's rules: the name pattern, the description limits, the parameter
ceiling, and no two commands answering to one name. All fourteen pass today. It is the first test
here that checks something Discord enforces rather than something this code does.

### A privileged intent that nothing used, and what it quietly turned on

`build_intents` asked for `members`, and the comment beside it and the README both said pinging a
GitHub user needed it. Neither was true. A mention is a user-id mention string built from a
`user_links` row and resolved by Discord on receipt, and the one thing that reads a member is the
permission gate, which reads `interaction.user`: discord.py builds that from the interaction
payload, and its roles resolve against the guild role cache that arrives under the ordinary
`guilds` intent. Verified against the installed library rather than from memory.

Asking anyway cost three things. It is a Developer Portal toggle that stops the process starting at
all when it is missed, which is a first-run failure over a capability nothing used. It needs
Discord's approval past a hundred servers. And discord.py reads it as a request to chunk, so the
entire member list of every server is pulled over the gateway before READY fires and then held in
memory, while READY is exactly what the delivery worker waits for before it will write anything.
Removed, with tests pinning that it and the chunking stay off.

### A label write that GitHub answered 200 to and never wrote

Renaming a repository on GitHub, or transferring an issue, makes every request to the old path a
301. Checked against the live API rather than assumed: `/repos/facebook/jest/issues/1/labels`,
which is the exact endpoint the label writes use, answers 301 pointing at
`https://api.github.com/repositories/15062869/issues/1/labels`.

Following redirects was turned on for reads in an earlier round, where an unfollowed one reached
the user as "GitHub could not be reached". On a write it is worse than not following at all:
httpx re-issues a redirected POST as a bodyless GET, so putting a label on the renamed repository
fetched the label list, was answered 200, and wrote nothing.

Nothing downstream could tell that from success. `/set_in_review` replied that it worked, the
status went into the row and was rendered into the thread, and no later delivery re-derives status
from labels, so nothing ever noticed or repaired it. The removal beside it is a DELETE, which is
not downgraded and does land, so the item is left with its old status label stripped and no new
one. From the board poller the same call is recorded as carried through, so no later poll retries
it either.

Nothing corrects the stored repository name until an item webhook arrives, and no `repository`
event is registered at all, so the stale name is ordinary rather than rare. Writes now follow
redirects themselves, keeping the method, and refuse to follow one off the API host rather than
hand the token to wherever it points. A chain that never resolves still comes back as "GitHub
returned 301", which is loud and retryable.

Worth recording how it hid: the helper that builds a client for the GitHub tests did not turn
redirect following on, so no test could see the transport do the thing that caused this. The helper
now builds the client the way the real one is built.

### Two replicas, one brand-new item, and a review request deleted

The row lock added last round is the whole staleness mechanism: it is what makes the second sync of
one item re-read the row and answer "superseded". A brand-new item has no row to lock, and the
comment beside the lock claimed `get_or_create` covered that case. It does not. It stops a
duplicate key error and then hands back the other transaction's row, unlocked, with staleness never
decided.

A pull request opened with a reviewer already on it is two deliveries milliseconds apart. With two
replicas the queue hands one to each, as designed, and both used to answer "not superseded". The
one carrying the opened event then wrote its whole payload over the other's, and `replace` deletes
the rows of anyone the payload does not list, so the reviewer's row went with it and took
`notified_at` and `requested_at` with it. Run end to end with two worker processes and a real
database: on one replica the reviewer is pinged once; on two, nobody is pinged at all, both
deliveries are recorded processed with no error, and later an ordinary label event re-inserts the
reviewer with `notified_at` empty and pings them for a request nobody made.

The question is now asked in `_write` as well, at the only point a new item can be asked about at
all. It works because the insert is what serialises the two: on a conflict it waits on the unique
index until the other sync commits, so by the time it returns, that sync's timestamp is on the row.
`get_or_create` holds the row it reads back, since every caller of it writes. The rename follow
moved below the item for the same reason: until the row is there, whether to believe the payload is
not known yet.

The same hole in the other direction left a merged pull request stored as open, with the newer
timestamp on it, which nothing later corrects.

### The rebuild the note path could ask for only once

Last round gave the note path a way out of a deleted thread: ask the item's own sync to build a new
one. The ask was made from the branch that clears the dead pointer, and that branch cannot be
reached twice. Every attempt after the first stopped one step earlier, at the item with no thread,
where nothing asked for anything.

So one rebuild that failed for any reason ended the item's mirror. A 502 from GitHub, a spent rate
limit, or the worker's delivery deadline landing inside the rebuild is enough. That note burns its
sixteen attempts, every later comment and review on the item goes the same way, and the item has no
thread until an unrelated item event arrives, which for a closed issue or a merged pull request is
never. The docstring on the ask said the opposite in as many words: "a rebuild that cannot happen
now may well work on the attempt after". The line above it made sure it could not.

The ask moved to `mirror`, where both ways of having nowhere to post arrive, so it repeats for as
long as the delivery does. Verified against the database: with a rebuild that fails once and works
after, the old order asked once, posted nothing, and left the pointer empty across four attempts.

### A Discord outage arrived as somebody else's exception

The two lookups in the thread gateway caught a missing thread and a refused one and let everything
else through as a raw discord.py exception. That includes a 503, and a 500 or 502 that outlived
discord.py's own five retries.

Not a cold path. discord.py drops a thread from the guild cache the moment it archives, so the
fetch is the only route to exactly the archived thread the write path exists to reopen. Two things
went wrong when it fired. `delete` documents itself as best effort and suppresses this project's
gateway error, so a raw one walked straight through it and took down a sync whose useful work was
already done, costing a delivery a retry it did not need. And the command replies match on this
project's errors, so a Discord outage was answered with "something went wrong here, it has been
logged", which is the same unhelpful answer an earlier round fixed for GitHub.

Both lookups translate it now. The order of the arms is the whole distinction, since a refusal is
itself an HTTP exception, and a test says so.

## A seventh look, at the first hour on a real server

Same question as the sixth, pushed further out: not what the code does, but what an operator
running this for the first time actually meets. Six lenses again, over the first ten minutes on an
empty database, real GitHub payloads rather than the hand-built fixtures, Discord's own limits,
the process after a week, what GitHub-authored text does to a message, and being killed and
restarted mid-flight. Each finding below was reproduced, then handed to somebody told to refute
it; three did not survive that and are not here.

### Markdown after a link went to Discord unescaped

`as_plain_text` is the only thing standing between anything anybody writes on GitHub and Discord's
renderer. It escapes one character at a time, except for one alternative in the pattern that
matches a whole span: the `[text](url)` link form. That one is greedy, so on a line carrying a
link it runs from the first bracket to the last closing parenthesis on the line and puts a single
backslash in front of all of it. Every marker in between ships live.

A pull request titled `Fix [regression](https://github.com/o/r/issues/3) in **/*.py (again)` is a
link to an issue, a glob and a parenthetical, and it put an odd number of bold markers into a
metadata block built entirely out of matched pairs. Bold runs past a newline in Discord, so every
label below the title paired with the value of the field underneath it, and whoever wrote the
title chose where that started. The same hole let a code fence through, which opens a block that
runs to the end of the message and swallows the link back to GitHub with it.

The earlier round that turned `ignore_links` off closed the other half of this and the test that
pins it uses a bare URL, so it never touched the link form. Breaking the bracket away from the
parenthesis stops the alternative matching at all, which leaves every marker to be escaped one at
a time like the rest. Six titles of that shape are pinned now, against the escaping itself rather
than against the rendered block, where the block's own markers hide the leak.

### A forum that demands a tag was mapped happily and then refused every thread

A forum channel can be set to require a tag on every post. Nothing here picks one, because which
tag a pull request belongs under is the server's business, so Discord refuses every thread the bot
tries to open in such a channel. `/register` and `/set_channel` both accepted one: the guard asked
whether the channel was a kind that can hold threads, and a forum is.

The refusal then arrives as a 400, which the queue reads as worth retrying, so the first pull
request burned sixteen attempts over two hours and was dropped with one log line. Nobody is told
at any point, and every item after it does the same. Both commands now ask a single question that
covers both cases and answers in a sentence naming the checkbox to turn off.

The claim that Announcement channels fail the same way did not survive: an announcement channel
really is a `discord.TextChannel`, really does pass the guard, and really is offered by the
channel picker, but nobody established that Discord refuses the thread type rather than coercing
it, so it stays unrecorded rather than guessed at.

### A team ping that notified nobody

Discord notifies a role's members only when the role is mentionable or the sender holds Mention
@everyone, @here, and All Roles. Roles are created without that flag, and the permission is not in
the list the README asks for, so on an ordinary server the mention `/link_team` promises rendered
in the thread as a blue pill and reached not one person.

This is the one moment the team feature exists for, and it looks exactly like it worked. It does
not get a second chance either: the ping is claimed before it is sent and stamped as spent whether
or not anybody read it, so every review request that goes past before somebody notices is silent.
User links are unaffected, since a user mention needs no permission, which is why the two look
identical in testing.

`/link_team` now says so in its answer, and the README says so beside the setup steps. A warning
rather than a refusal: the link is worth having either way, and it is one checkbox to fix.

### Shutdown needed fifteen seconds and the container gave it ten

Two shutdown budgets were each written against Docker's ten second default, in different files,
neither aware of the other. The worker grace waits five seconds for the delivery in hand; the
cancellation then unwinds into the thread binding, which waits up to ten more for a thread Discord
has been asked for and not yet answered about. They run one after the other, and neither the
Dockerfile nor the compose file set a stop timeout.

Killed part way through, the worker never hands back the rest of its leased batch, so up to nine
other deliveries sit locked until their fifteen minute lease lapses. The replacement process starts
clean, polls a queue that looks empty, and those pull requests get no thread for a quarter of an
hour with nothing in either log saying why. That is the exact regression an earlier round fixed by
adding the hand-back, made unreachable in the case it was written for. Docker Desktop is worse than
the documented default and stops a container after about a second and a half.

The compose file now allows thirty seconds and says where the number comes from. A test reads both
waits out of the code and the allowance out of the deployment, so raising either budget without
raising the allowance fails rather than quietly going back to being killed.

### A dead worker left a live process that nothing would restart

The delivery worker dying takes the whole point of the process with it, and the only thing that
happened was one line in the log. Uvicorn kept serving, the endpoint kept answering 200 to
deliveries nothing would ever work, and the container sat there.

`/health` reported this correctly and the image's health check read it correctly, and that changed
nothing, because a container restart policy watches the exit code and never the health state. An
unhealthy container that has not exited is a container that waits for a human. An earlier round
recorded this as fixed on the strength of the health check now asking `/health`, which was never
what decided it.

A task the process cannot do its job without now asks the process to stop, by sending it the same
signal an orchestrator would, so the ordinary shutdown still runs and the delivery in hand still
finishes. The worker and the gateway are wired that way; the poller is not, because everything the
webhooks bring still works without it. Half of what ends these tasks is fixed by a restart, a
rotated token first among them. Nothing was being lost while it sat there, which is the one thing
the original report had too high: the endpoint's 200 is the right answer, and those rows are
pending, not failed, so they are worked within milliseconds of the next start.

### A blank database URL failed before anything written to be helpful about it

`build_engine` runs ahead of uvicorn, so a URL that will not parse fails there rather than at the
startup check written to say what is wrong with a database. SQLAlchemy's own words for it name
nothing an operator can go and change, and blank is the shape it usually takes: an empty
`SHANNON_DATABASE_URL=` in a copied `.env` reads as a value that was set, and pydantic takes it.
It now names the setting and shows the shape of a working one.

## An eighth look, at what a person actually does with it

Six lenses over the trust boundary, the eight workflow commands run in every order somebody
really would, the setup being changed after things are already mirrored, the board poller against
a board that is not the one in the tests, the migrations against a database with rows in it, and
a command and the delivery worker touching one item at once. Three findings did not survive being
handed to somebody told to refute them and are not here.

Two more came out of reading the reply table rather than from a lens.

### A missing permission read as a refusal, with a Discord error code attached

The reply table splits GitHub's errors carefully: a spent rate limit says when to come back, a
refused token says an admin has to look, and only a real outage says GitHub could not be reached.
It did nothing of the kind for Discord's. A channel that has gone and a permission nobody granted
are both a gateway error, so both fell through to "Discord refused the update", followed by
whatever discord.py had said.

What that looked like in a thread was `Discord refused the update. Discord will not let the bot
lock the thread: 403 Forbidden (error code: 50013): Missing Permissions`, which names no
permission and no fix, and `Discord refused the update. Channel 12345 is not there`, which names a
snowflake and no action when the fix is one command. A missing permission is the likeliest thing
to go wrong on a server nobody has run this on before. Both have their own row now, above the
general one, and a test moves the general row up and watches them break, because the ordering is
the whole thing.

### A command that did most of its work reported total failure

Locking is deliberately the last step of a status change, because everything before it is worth
keeping when it is refused: the labels are on GitHub, the status is in the row, the thread already
says so. It raised anyway, so the person who ran `/set_done` was told the command had failed and
would reasonably go and check whether the item was done. The two readings are one Discord
permission apart.

The refusal is carried back now instead of raised, and the answer says both halves: the item is
where it was put, the thread could not be locked, that is usually Manage Threads, and running it
again gives the lock another go. The last part already worked and nothing had ever said so. Only
the lock is survivable this way; anything failing before it still fails the whole command, since
then nothing did happen.

That change had a consequence worth recording, because it was caught by a test rather than by
thinking. The board poller depends on `set_status` raising: a raise is what stops it writing the
card's new column down, and not writing the column down is the only thing that brings the card
round again. Swallowing the refusal silently reintroduced a defect this file already records as
fixed, a card dragged to Done keeping an open thread for ever. The poller now reads the refusal
off the outcome and leaves the column alone, which is the same decision made in the open.

### A refused unlock could never be tried again

The other direction of the same step, and worse. A pull request moved out of DONE has to have its
thread given back, and nothing but this path ever does it: `PullRequestPolicy.locked` returns None
on every sync, so no webhook, no `/pr`, no board move touches a pull request's lock. If Discord
refused that one call, the row already said the new status, so the branch that touches the lock
was never reached again, and the repeat that exists to retry a failed lock was gated on the status
being DONE. One 503 left a reopened pull request shut against the discussion it had just been
reopened for, permanently, while every later command answered that it was already where it was
being put.

The repeat now puts the lock where the status says it belongs in both directions. A closed issue
cannot reach it asking to be unlocked, because the guard above refuses any status but DONE for
one, so the only thread this ever opens is one this path shut. The reply distinguishes the two
directions as well: a thread that would not lock is untidy, and a thread that would not unlock is
one nobody can reply in.

### Two commands at once left an open item with a shut thread

Whether to give the thread back was decided from the status read at the top of the command, three
GitHub round trips before the write. Two commands overlapping across that window both decided from
a row neither of them still had: a pull request at READY_FOR_MERGE, one reviewer marking it done
and another putting it back into review, or the board poller doing the second. The one that was
not finishing the item read a status that was not DONE yet, so it never asked for the thread back,
while the other locked it last. The item was left reading IN_REVIEW with its thread shut, both
callers were told they had succeeded, and nothing lifted it.

The status is now read and written under the row's own lock, and the answer that comes back is
what the lock decision is made from. The test that pins it holds the second command at the read
between its own look at the row and its write, using the fake GitHub rather than a sleep: written
against a timer it caught the defect two runs in three, which is worse than not having it.

### A board that could not be read wrote itself over everything

A board's Status field is matched by name, so renaming it is enough to make every card come back
carrying no column at all. That is indistinguishable here from a board where nobody has picked a
status yet, and the poller wrote down what it was told: the empty string over every remembered
column, on every card, in one pass. The poll after the field came back then read the whole board
as having moved into a column and drove all of it through the status commands, stripping whatever
label a person had set by hand while the board was unreadable.

Two things kept it going. The field ids are looked up once per board and kept, and an answer
carrying Title without Status was kept like any other, so a renamed field was read that way for
the life of the process rather than for one poll. That answer is no longer remembered. And the
poller now refuses the one write that does the damage: a card that had a column and now reads as
having none keeps the one it had. Nothing is lost in the case that guard is wrong about, since a
card with no column carries no status to move to and is passed over either way, and the memory
that is kept is a column the card has left, which the next real move still differs from.

### The schema check could not see the thing the models said it checked

`ix_webhook_events_live` is what keeps the delivery lease off a full table scan, and its predicate
is why it stays small however long deliveries are kept. The comment beside it said the string had
to match the migration byte for byte or the schema diff in `test_migrations` would fail. It would
not: alembic's PostgreSQL comparison ignores an index's WHERE clause, so widening the predicate,
narrowing it or deleting it outright all left that test answering with no differences.

Nothing was actually wrong with the schema, which is why nothing showed. The predicate is now read
back out of `pg_indexes` and compared against the statuses `live()` names, so the claim the models
make is one something enforces.

### `/set_channel` answered from a row nothing was placed by

`/register` maps pull requests and nothing else, so until somebody runs `/set_channel issues`,
issue threads open in the pull request channel through the fallback the issue policy declares.
That is the shipped default and the state most servers are in. The first `/set_channel issues`
then answered from the issue row, which does not exist, so it said nothing at all about the issue
threads sitting in the pull request channel.

Discord cannot move a thread between channels, so every item already tracked keeps the one it has,
and telling the admin where that is exists precisely so they do not go looking for threads that
never went anywhere. The service is handed the fallbacks now, read off the sync policies rather
than restated, and answers from where the threads actually went.

## A ninth look, by changing the code to see what nobody notices

A different method from the eight before it. Six lenses were sent out after logic errors
specifically, and five of them died on a session limit before reporting; the one that finished
found nothing, having gone through the label parsing, every dict keyed by an enum, the orderings,
four hundred combinations of the status commands and a hundred and eighty of the board poller.

So the work here is mutation testing. One operator at a time is changed in place, the tests are
run, and whatever passes anyway is a decision nothing pins. Replaced by character span rather than
by re-emitting the module, because `ast.unparse` strips every comment, and a run killed part way
through would leave the file looking like a machine wrote it. One run was killed, and what it left
was a single `0` turned into a `1`, visible in a one-line diff.

### The module that decides what Discord accepts was pinned by nothing

Nine of the eighteen logic changes possible in `safe_text` went unnoticed by the whole unit tier.
Inverting the comment preview cut, so that short comments are truncated and long ones are not:
unnoticed. Moving Discord's message limit from 2000 to 2001: unnoticed. Turning the boundary at
exactly 2000 from inclusive to exclusive: unnoticed.

The reason is worth writing down because it is a shape rather than an accident. What covers that
module is the block formatters and the property tests, and those assert that the rendered length
is at most `MESSAGE_LIMIT`, comparing the output against the very constant that decides it. Raise
the constant and the assertion rises with it. Those tests could not have failed.

`tests/unit/discord_bot/test_safe_text.py` checks the constants against the numbers Discord
documents, written out separately, and both functions one character either side of every limit
they enforce. Killing the last three took a message whose lines land exactly on the budget, which
lines of equal length can never do: that is why three different one-character changes to the same
arithmetic all survived every other test in the file. Seventeen of the eighteen die now. The
survivor is the seven hundred character comment preview, a product choice with nothing external to
measure it against, so what is pinned is its relationship to the message limit rather than its
value.

### Three renderers of one title disagreed about whitespace

A title of nothing but spaces is truthy, so a check asking whether there is a title answered yes,
and the metadata block rendered a label with nothing after it. The other two renderers of the same
string already knew better: `thread_name` strips, and `TicketPolicy.thread_name` calls an untitled
card an untitled card. A draft titled with spaces opened a thread named "Untitled ticket" whose
first line named it nothing at all, and a field rendered blank reads as the bot having broken
rather than as an item nobody named.

The mapping layer refuses a title that is missing or empty outright, which is why this is the one
shape of it that gets through. All three agree now, and five kinds of whitespace are pinned.

### Two parsers could drop a note without saying so

`parse_comment_event` and `parse_review_event` both end by logging a warning when the note cannot
be read. Inverting the condition that decides whether to log went unnoticed by the whole unit tier
in both files: every refusal would have been silent and every success would have carried the
warning. A note refused there reaches nobody and leaves no row, so that line is the only thing
between "the comment never appeared" and knowing why. Both are pinned now, in both directions.

### What came back already sound

`labels.py`, `urls.py`, `permissions.py` and the four other webhook parsers have no unpinned
decision left in them at all. `mapping.py` has one, and it cannot matter: `split("/", 1)[0]` and
`split("/", 2)[0]` are the same expression.

Also recorded, because it cost time twice: a mutation result is only as good as the tests it was
run against. Four apparent survivors in `policies.py` and `formatting.py`, and one in the event
router, all die against the whole suite and were nothing but a narrow test selection. Half the
survivors in the fourth look's campaign were the same thing. The router one is worth a note of its
own: `EventRouter.handles` has no production caller, since the route asks `will_act_on`, and it
exists for the two tests that check every supported event has a handler. That is a method serving
a test rather than dead code, and it stays.

One methodological cost worth recording too. The mutation runs and the reading agents overlapped,
and one agent read a constant mid-mutation, reported 2001 where the file says 2000, and had to
check it against git and a live import before trusting either. Changing the tree under somebody
reading it wastes both of them.

## A tenth look, at logic that reads from the wrong side

The five lenses that ran out of quota last round, run again: the conditions in the service layer,
the arithmetic the delivery machinery does, every comparison between two moments, the set and dict
work in the stores, and whether the thread can be made to say something untrue. Everything below
was reproduced before it was touched, and one of the reports was refuted and is not here.

### A closed issue could be given an open thread

The rebuild path exists so that an item whose thread somebody deleted gets another one however
old the delivery that finds it. It refuses that delivery about everything else: the title stays as
stored, the state stays as stored, the status stays as stored, and the people stay as stored,
because a payload from before the close is wrong about all of them and a ping cannot be taken
back. The lock was the one thing still read off the payload.

So a closed issue whose thread was deleted, rebuilt from a delivery captured while it was open,
came back with an open thread on an item the row records as closed and DONE. Nothing else shuts
it: an issue's lock is set by exactly one line, reached once per delivery, and a closed issue has
no reason to send another. The requirements say a closed issue's thread is locked.

The lock is now decided where the payload and the row are both in hand, and asked about the item
as it is stored rather than as the payload describes it. The staleness guard that would have
blocked it is skipped for that answer alone, which is sound for the reason the guard exists: it
protects a decision made from a payload that may have been superseded, and a decision made from
the row has nothing to be superseded by. The block beside it had the same fault, recorded below.

### A rebuilt thread reported the payload it had just refused to believe

The same branch, and the same mistake twice over. Having decided not to believe a stale delivery
about the title, the state, the status or the people, it rendered the block from that delivery
anyway. A merged pull request whose thread somebody deleted came back saying `State: Open`, and a
closed issue came back with `State: Open` sitting directly above `Status: DONE`, contradicting
itself and contradicting the row it was built from in the same transaction. The thread was renamed
from the stale title to match.

The stale block is accepted elsewhere on the grounds that the next delivery corrects it, and that
is the premise that fails here. A merged pull request and a closed issue send no further events,
so this was not a window but the last thing the thread ever said.

The block and the thread name are now rendered from the item as stored, for the fields the row
holds, and beside each other so the two cannot describe different states. The people are left
alone: they live in their own table rather than on the row, and the empty mention map that branch
leaves behind is what stops a rebuild pinging whoever was on the item at the time.

### A draft whose first message was refused was never offered again

Opening a thread and writing the first message in it are two Discord calls and two permissions, so
a server that grants Create Public Threads and not Send Messages in Threads refuses the second
every time. The thread is real by then, and the sync attaches it to the row on purpose so that a
retry writes into it rather than opening another beside it.

Putting the card's timestamp back was skipped whenever nothing had been stored before, on the
grounds that a card with no timestamp or no thread is offered again anyway. That describes the row
as it was before the sync, not as the sync had just left it: the row holds both the card's
timestamp and a thread id, so neither escape applied. The card compared its own timestamp with
itself for ever, Discord kept an empty thread named after it with no block inside, and a draft has
no worker to retry it. One warning, then silence.

It now goes back to never seen, which is where a card that failed on its very first mirror
belongs, and the next poll writes the block into the thread that is already there.

### `/set_priority` could not repair what it had half done

The label goes on GitHub first and the row and the thread follow, so a run that fails in between
leaves the three disagreeing. The status half of the same service guards on both the labels and
the stored status before deciding a repeat has nothing to do. The priority half asked GitHub
alone, so the command that exists to put that right answered "already HIGH priority" and wrote
nothing, and nothing else ever rederives a priority: it stayed wrong until an unrelated event for
the item arrived, which for a merged pull request is never.

It asks both now. A repeat with nothing out of step still does nothing at all, which the
requirements are explicit about and which has a test of its own.

Recorded rather than fixed, because it needs that requirement changed rather than read
differently: when GitHub and the row agree and only the thread is stale, no repeat of any of these
commands rewrites the block. `/set_done` is the exception, because its repeat retries the lock.
The requirements say a duplicated command takes no action; making a repeat re-render would be an
action, and it is worth deciding on purpose rather than as a side effect of this.

### A full read reported as cut short

`get_pages` bounds itself at fifty pages, and warned about it from the loop's `else`, which runs
whenever the range is exhausted rather than only when a page was left behind. A list that ends on
exactly the fiftieth page was read whole and logged as truncated. Reproduced against the real
client: forty-nine pages, no warning; fifty pages, read whole, warned; fifty-one, read short and
warned. It now warns only when there was a next page it did not follow. A warning that fires when
nothing is wrong teaches whoever reads the log to skip the line, and the one time it means a board
is being cut off looks exactly like the times it does not.

### A login matched in a case the column never holds

`ItemAssignmentStore.release_notifications` matched on the login as given, while the three other
methods there that take logins all fold first and the column only ever holds folded values. It
works today because its one caller hands back exactly what the claim returned, which came out of
that column. Any other caller would have matched no row, and matching no row there means a ping
stamped as sent that nobody received and nothing revisits. Folded now, with a test that passes the
login in the case GitHub uses.

### A race test that only raced when the machine was quiet

Found by the coverage floor rather than by reading. Three tests in the concurrency file arranged
an overlap by holding a row, starting the other half as a task and sleeping two tenths of a
second. Run on their own they exercised the branch they were written for. Run in a full suite on a
loaded machine, the task had not reached the database at all by the time the sleep ended: the
holder committed first, the sync found the row where it looks for it, and the test passed having
gone down the other path entirely.

They passed either way, which is the worst way for a race test to be wrong, and the only reason it
surfaced is that one of those branches has nothing else covering it. They now ask PostgreSQL
whether anything is waiting on a lock rather than guessing how long that takes, and say so out
loud when nothing ever blocks. The same fault was fixed in the workflow race test earlier this
round, where it showed up as a mutation surviving two runs in three.

### What came back sound

The delivery arithmetic: sixteen attempts really are sixteen tries, the fifteen backoffs
`total_backoff` sums are exactly the fifteen the loop applies, and the give-up boundary is at the
sixteenth. The mention lookups: `/link` and `/link_team` fold on the way in and the renderers fold
on the way out, on both sides. The note keys are prefixed by kind, so a review and a comment
sharing a number cannot be taken for one another. The manual sync compares a link against the
stored name case-insensitively. The board poller normalises both sides of every column comparison.

A refuted one worth recording: the rebuilt block naming people as plain logins instead of mentions
is not a defect but a defence. Thread creation sends that block as a real message under allowed
mentions that permit users, so filling the mention map in would ping the stale set of people the
rest of that path exists to avoid pinging.

## An eleventh look, mutating the service layer

The ninth look mutated the pure modules, where a test run costs a second. This one does the rest,
where a run needs PostgreSQL and costs a minute, so each module was given a test selection first
and the selection was only used once it was shown to cover the module. That discipline is the
whole reason the results below are worth anything: twice already in this project a batch of
apparent survivors turned out to be nothing but a narrow selection.

Six modules, a hundred and twenty-six one-token changes. Two defects worth a test, five decisions
that nothing pinned, and a list of survivors that are survivors for good reasons.

### The guard that let the wrong delivery through

`items.py` gave up one survivor and it was the useful kind. The create-race guard reads
`if superseded and item.discord_thread_id is not None`, and it could be changed to `or` without a
single test noticing, because every case the suite covered had both facts agreeing: an ordinary
creation has neither, and the loser of a race for a brand-new item has both.

The case nothing exercised is the one where exactly one holds. Whichever sync reaches the insert
second is not necessarily the one carrying the older payload: GitHub sends several events for a
new item together and the queue hands them out in parallel, so losing the race says nothing about
knowing less. Under `or` that delivery is discarded whole, and nothing revisits it, because the
row it would have corrected already carries a timestamp newer than the one that wrote it. Now
tested, from the side the suite had never approached.

### The line that decides whether a backlog drains

Four separate one-character changes to
`if handled < self._settings.batch_size and not self._stopping` went unnoticed, which is every
way there is to write that line wrong. It carries three decisions and nothing pinned any of them:
that a full batch goes straight back round, that a short one waits, and that a stop is not made to
wait first.

Each of the three matters somewhere different. Sleeping after a full batch caps the queue at one
batch per interval however far behind it is, which is exactly the moment it must not: a repository
that was busy while the bot was down comes back as a burst. Going straight round after a short one
is a hot loop against the database for as long as the queue is empty, which is nearly always.
Sleeping when a stop has already arrived spends a whole interval before looking at the flag again,
which at the shipped two seconds is invisible and at anything longer is a container killed part
way through the shutdown it was given time for.

Three tests, one for each. Two of them are the same test with the comparison the other way round,
which is the point: the full-batch case cannot tell `<` from `>`, because at exactly the batch
size both are false.

### The line that says the queue is running without Discord

An earlier round gave the wait for the gateway an end, so a client that never connects no longer
parks the queue for the life of the process. What it does instead is go ahead without Discord, and
the one line that says so was not pinned: dropping the negation in front of it left the decision
identical and the log silent. Every delivery then fails with a gateway error that names Discord
rather than the choice that was made about it, and nothing anywhere connects the two. Pinned now,
in both directions.

### What survives, and why each one is left alone

Twenty-one survivors are recorded here rather than chased, because a mutant is a question and
these have answers.

Eleven are constants with no external truth to measure against: five worker settings, the comment
preview length, the exponent clamp. A test asserting `retention == 7 days` restates the line above
it. The one that would have been worth pinning says so itself: `error_limit` is documented as a
readability limit against a `Text` column rather than a schema one, so overshooting it breaks
nothing.

Four are boundaries between two moments that both come from the database clock, in the lease and
the prune. Making them equal would mean freezing that clock, and `<=` is the right side of each:
a delivery due now is due.

Two are `split("/", 1)` against `split("/", 2)` on a name with exactly one slash, which is the
same expression. Two more are the second half of an `or` whose first half already covers every
reachable case, one of them needing a `repositories` row deleted between two statements of the
same read, which nothing can do because the foreign key cascades and there is no unregister.
The last is cancelling a future that is already done, which is a no-op either way.

### A note on the method

The coverage floor cannot find any of this. Branch coverage asks whether an `if` went both ways.
It never asks whether each half of an `and` mattered, and both of this session's real mutation
findings were compound conditions where every test happened to exercise the halves together.

Two of the mutants took the machine down while being checked. Changing `<` to `>` on the poll
decision turns an idle worker into a hot loop against PostgreSQL, and the run has to be killed
rather than waited out. The harness replaces one operator by character span rather than rewriting
the module, so what a kill leaves behind is a one-line diff with every comment intact, which is
the only reason that was cheap rather than expensive.

## A twelfth look, before real people use it

Six lenses over ground the eleven before them had not touched: the whole journey a person takes,
driven end to end and read as a person rather than as a programmer; more than one server; what the
log says to somebody at three in the morning; what runs out first; whether every service is wired
to the thing it should be; and the documentation against what the code does. Five reports were
refuted, two of them after their mechanism had been reproduced, which is the right outcome for a
mechanism that is real and a consequence that is not.

### A repository name is not an identity, and every write used one as if it were

The most serious thing found in twelve rounds. GitHub frees an `owner/name` the moment a
repository is renamed, transferred or deleted, and anybody can take it. The stored name goes stale
by design: nothing corrects it until an item webhook arrives, and a repository renamed away sends
none, so a row can name a path that belongs to a stranger for as long as the server lives.

Every write the workflow commands make was addressed by that string, and nothing compared what
came back against the row it came from. Reproduced against a live database: the status label is
written onto the recycled repository's item of the same number; the re-render then resolves the
fetched snapshot by its own repository id and opens a thread in whichever server registered it;
`/set_done` locks that thread rather than the one the command was run in. The reviewer is told it
worked, their own thread never changes, and every repeat afterwards answers that the item is
already where they put it.

`/pr` and `/issue` had the same hole through a different door. The confirmation they already carry
settles a genuine rename by asking GitHub for the id, and it is only reached when the name
disagrees, so a name that still matches walked straight past it.

Both now compare the fetched snapshot's repository id against the registered one, which costs no
API call because the snapshot already carries it, and both say what actually happened rather than
refusing blankly.

### A permission is not a bad moment

The board poller deliberately does not write a card's column down when its move could not be
carried through, because the column is the record of a move having landed and not writing it is
what brings the card round again. That is right for a Discord blip. It is wrong for a missing
permission, which no amount of coming round again will grant.

With the bot invited without Manage Threads, every card ever dragged to Done joined a set that was
retried on every poll and never left it: a GitHub read and a Discord call each, once a minute,
growing with the team's throughput, against the same rate limit the board read and every command
draw on. The workflow now reports whether a refusal is the kind that can be waited out, which is a
distinction the error hierarchy already makes and only this caller needed, and the poller writes
the move off and says once what to grant.

### The board read handed the same card back twice

A board is paged by cursor and a cursor is not a snapshot: GitHub documents that a list edited
while it is being paged can hand the same row back on two pages, which is exactly what a board
somebody is dragging cards around on is. The draft half of the poll guarded against it and spent
six lines saying why. The wrapped half, in the same function, did not, although the reason given
is a property of the read rather than of drafts.

Both halves judge a card against state read once for the whole board and never written to, so a
second copy is judged against the state before the first was acted on. Measured: one card listed
twice cost two GitHub reads of the item instead of one, on every poll that saw it, and the pass
reported two moves where one had happened. The dedupe now happens to the read, where its reason
lives, and the draft half no longer needs its own.

### The health check was the one thing an outage silenced

`/health` probes the database behind a five second deadline, added by an earlier round so that a
database that has stopped answering cannot park every health check behind it. The deadline does
not hold. The engine pre-pings on checkout, which is right for work that must not be handed a
connection that died in the pool; when the deadline cancels that pre-ping, SQLAlchemy treats the
connection as failed and terminates it, and terminating an asyncpg connection opens a second
socket to send the cancel and waits on that one with nothing bounding it.

Measured against a frozen database: the first health check after the outage began returned nothing
for eleven minutes, and every later one queued behind it, while the rest of the application
answered in milliseconds. The probe now asks through an engine of its own with no pool and no
pre-ping, so every probe opens its own connection and the deadline is the only thing deciding how
long it waits. The engine everything else uses still pre-pings, because its needs are the
opposite.

### The one failure with nothing to say

`str(TimeoutError())` is the empty string, because asyncio raises it with no arguments, and that
is the one database failure whose reason was interpolated into a message as nothing at all. A
refused connection, a wrong password, a missing database and a DNS failure all fill their own; an
unanswered connection, which is a dropped packet or a security group nobody opened, read in the
log as a colon with nothing after it. At startup the traceback underneath makes up for it. At
runtime nothing does: the health probe's line is the only place an outage is explained, and it
said nothing.

### The token the README understates

The configuration table calls `SHANNON_GITHUB_TOKEN` the "REST token for `/register`, `/pr` and
`/issue`", which are three reads, and the board note asks only for project read access. Every one
of the eight `/set_*` commands and every board-driven move writes a label through that same
client. A token granted exactly the access the README asks for leaves all of them failing, and the
reply blames the read that had just succeeded. The row now says it needs write access and which
commands need it.

### Considered and left alone

Two first-run messages are true in a way that reads oddly and are recorded here rather than
changed.

`/set_channel issues` on a server registered a minute ago says "Threads already open stay in
<#the-channel-you-just-registered-in>". Nothing is open. The clause is a statement about where
open threads stay, vacuously true of none, and it fires whenever any previous mapping existed
rather than because of the fallback: `/set_channel pull requests` on the same fresh server says it
too. Its wording was chosen deliberately over "Moved from" in an earlier round and is pinned by a
test.

`/set_channel project tickets` answers "will now appear in <#X>" on a deployment whose board
number is still zero, where nothing will ever appear. The setting's own comment in `.env.example`
says so, and a test exists to keep it named there, so this is a discoverability problem that was
already found and fixed once. What is left is that the reply itself does not know, and telling it
would mean handing the command a setting it does not otherwise need.

## A link that could never work, recorded as though it had

`/link` is the first command every person on a server runs, and until now it recorded whatever
they typed. The pattern it checked against accepted names GitHub cannot issue, and of course
accepted any correctly shaped name that is simply not an account.

What made it worth fixing is the shape of the failure rather than its likelihood. Somebody who
has not linked is named in plain text in the thread, deliberately and by design. So a login with
a typo in it produces exactly what an unlinked person produces: plain text in the ping, plain text
in the reviewer line of the block, and nothing at all in the log. There is no way for the person,
the server admin or the owner to tell the two apart, and the person simply never hears from the
bot again.

Proven before it was touched, through the real command:

    'mona--lisa'                     -> Linked GitHub user mona--lisa to <@555>.
    'monalisa-'                      -> Linked GitHub user monalisa- to <@555>.
    'definitely-not-a-real-account'  -> Linked GitHub user definitely-not-a-real-account to <@555>.

and then, with `monalisaa` linked and a review requested from the real `monalisa`:

    Review requested from monalisa.
    **Reviewers:** monalisa

The login is now checked against GitHub. `GET /users/{login}` is public, so it answers with no
token set and answers correctly for somebody who can only be seen through a private repository,
and it is one call on a command each person runs once. Only "not there" becomes an answer:
anything else GitHub says is a reason the question could not be put, and refusing sends the person
back in a minute rather than binding a name nothing will ever match.

Its own protocol at the consumer rather than the whole client, so binding a name to an account
cannot reach anything that reads a pull request or writes a label.

The pattern was tightened to GitHub's real rule at the same time, single hyphens and never leading
or trailing, so the shapes GitHub cannot issue are refused without a call that could only say no.

Two things this does not cover, both recorded rather than fixed. `/link_team` has no equivalent: a
team slug is only checkable through an endpoint that is not public and needs organisation scope
the token may not have. And verifying at link time says nothing about a login that changes hands
afterwards, which is a separate defect on the same table.

## A link that could never work, recorded as though it had

`/link` is the first command every person on a server runs, and until now it recorded whatever
they typed. The pattern it checked against accepted names GitHub cannot issue, and of course
accepted any correctly shaped name that is simply not an account.

What made it worth fixing is the shape of the failure rather than its likelihood. Somebody who
has not linked is named in plain text in the thread, deliberately and by design. So a login with
a typo in it produces exactly what an unlinked person produces: plain text in the ping, plain text
in the reviewer line of the block, and nothing at all in the log. There is no way for the person,
the server admin or the owner to tell the two apart, and the person simply never hears from the
bot again.

Proven before it was touched, through the real command:

    'mona--lisa'                     -> Linked GitHub user mona--lisa to <@555>.
    'monalisa-'                      -> Linked GitHub user monalisa- to <@555>.
    'definitely-not-a-real-account'  -> Linked GitHub user definitely-not-a-real-account to <@555>.

and then, with `monalisaa` linked and a review requested from the real `monalisa`:

    Review requested from monalisa.
    **Reviewers:** monalisa

The login is now checked against GitHub. `GET /users/{login}` is public, so it answers with no
token set and answers correctly for somebody who can only be seen through a private repository,
and it is one call on a command each person runs once. Only "not there" becomes an answer:
anything else GitHub says is a reason the question could not be put, and refusing sends the person
back in a minute rather than binding a name nothing will ever match.

Its own protocol at the consumer rather than the whole client, so binding a name to an account
cannot reach anything that reads a pull request or writes a label.

The pattern was tightened to GitHub's real rule at the same time, single hyphens and never leading
or trailing, so the shapes GitHub cannot issue are refused without a call that could only say no.

Two things this does not cover, both recorded rather than fixed. `/link_team` has no equivalent: a
team slug is only checkable through an endpoint that is not public and needs organisation scope
the token may not have. And verifying at link time says nothing about a login that changes hands
afterwards, which is a separate defect on the same table.

