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
  shapes each. They already held up, which is worth knowing rather than assuming: these read
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

Worth saying how this was found: not by reading, but by running `uv sync --python 3.12` and the
suite against it, which is exactly what the CI job does and what nobody had watched it do.

