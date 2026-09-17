# shannon

GitHub repository activity, mirrored into Discord threads

**shannon** binds one GitHub repository to one Discord server. Every pull request and issue gets a
thread, kept in step as the item changes, with comments and reviews underneath it. Webhooks are
answered as soon as they are checked and written down. Everything slow runs behind a queue, since
GitHub allows ten seconds and never redelivers anything it recorded as failed.

## What it does

- **Threads.** One per item, opened on the first event and edited in place after. The metadata
  block carries name, type, state, link, author, assignees, reviewers, status, priority, tags,
  last updated, and the description the item was opened with where there is one.
- **Comments and reviews.** Quoted into the item's thread with a link back. Edits and deletions
  are not mirrored, so a thread records what was said at the time.
- **Tags in a comment reach people.** `@someone` in a comment body becomes a real Discord mention
  where that login has been linked, and `@org/team` becomes a role mention where that team has.
  Anybody unlinked is still named in plain text. At most ten per comment are mentioned, because
  without a limit one comment could ping every linked member of the server.
- **Pings.** Reviewers and assignees are told once each, as mentions where the account is linked.
  The claim is taken before the message goes out and handed back if it fails. When a thread is
  first opened the metadata block carries those mentions and is a real message, so it is the
  ping; the separate line is kept for people added later, when editing the block would reach
  nobody. Anybody who has run `/mentions off` is still named in all of it, as a mention Discord
  renders and does not ring.
- **Lines in the thread.** A tag moving says so, priority coloured by level and the five workflow
  statuses told apart from ordinary labels. A tag the opening block already listed says nothing:
  GitHub sends those as their own deliveries a moment after it. Closing, merging or reopening
  posts a header saying what became of the thread, and a finished item's thread is shut: locked
  and archived out of the channel, and opened again if the item is. Both exist because a Discord
  edit is silent: it posts no message, notifies nobody, and does not bump the thread, so a change
  that only moves the block looks from the channel like nothing happening.
- **Manual sync.** `/pr` and `/issue` pull one item from the REST API, for whatever the webhooks
  missed. `/refresh` does the whole backlog: every open item with no thread gets one, quietly.
- **Late deliveries.** GitHub does not guarantee order and retries land whenever. A high water
  mark per item stops an old delivery undoing a newer one.

## How a delivery becomes a thread

1. `POST /webhooks/github` checks the signature against the raw body and decodes it. Nothing here
   touches Discord.
2. Events and actions the bot does not act on are answered `ignored` without a row, so pushes and
   stars stay out of the queue.
3. The delivery is claimed by its `X-GitHub-Delivery` id and written to `webhook_events` with its
   payload. A repeat answers `duplicate`, anything new answers `accepted`.
4. The worker leases a batch with `SELECT ... FOR UPDATE SKIP LOCKED`, oldest first, one at a time
   so two events for one item keep their order.
5. One transaction resolves the repository and channel, upserts the item, records who is on it and
   renders the metadata. Discord is called after it commits, never inside it.

A failure at any point reschedules the whole delivery, so every step is written to be repeatable:
upserts rather than inserts, compare-and-swap on the thread pointer, claims taken before posting.

## Install

Python 3.12 or newer, Docker for the database, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev --locked        # runtime and dev dependencies
cp .env.example .env                # fill in before the next line, not after
docker compose up -d --wait db      # PostgreSQL 17 on localhost:5433
uv run alembic upgrade head
uv run shannon
```

Startup refuses to open the port until the database answers and has been migrated. Without a
Discord token it still serves the endpoint and works the queue, and warns that it is doing so.

## Connecting it to GitHub and Discord

Neither side is configured by this repository, and the bot cannot do either for you.

**The GitHub webhook.** Repository settings, Webhooks, Add webhook. The payload URL is wherever
this is deployed plus `/webhooks/github`, the content type is `application/json`, and the secret is
the same string as `SHANNON_GITHUB_WEBHOOK_SECRET`. An unset secret answers 500 to every delivery
rather than waving them through, so a mismatch shows up at once rather than quietly.

Choose individual events, and choose these four:

```text
Pull requests
Issues
Issue comments
Pull request reviews
```

Anything else is answered `ignored` without a row, so subscribing to more costs nothing but noise.
GitHub's Recent Deliveries page is the first place to look when nothing appears in Discord: 401 is
a wrong secret, 500 is an unset one, and a 200 answering `ignored` means the event arrived and this
bot does not act on it.

**The Discord bot.** No privileged intent is needed, so there is nothing to turn on under Bot in
the Developer Portal and nothing for Discord to approve. Pinging somebody works from the account
map `/link` builds, and reading somebody's roles works from what Discord sends with the command
itself. Invite it with the `bot` and `applications.commands` scopes and these permissions:

```text
View Channels
Send Messages
Send Messages in Threads
Create Public Threads
Manage Threads
Read Message History
```

Two of those are easy to miss and both fail after everything looks fine.

`Read Message History` is needed to edit the metadata block. The block is one message, written when
the thread opens and rewritten on every later event, and rewriting it means reading it first;
Discord counts that as reading history even though the bot wrote it. Without this the thread
appears once, correctly, and then never changes again, and every later delivery for that item is
refused as a missing permission rather than retried.

`Manage Threads` is refused by `/register` and `/set_channel` if it is missing, which is the only
one of these checked at the door. It is what shuts a finished item's thread, meaning locked
against replies and archived out of the channel, which is what Discord's client calls closing a
thread. Both halves go in one edit, so a server without it gets neither and its threads simply
stay open.

It is also what reopens a shut thread to write in it, so a comment on a closed issue needs it as
much as the close did. Grant it before anything closes. Adding it later is enough on its own,
because the row remembers that a thread is owed a shut; taking it away afterwards is not, because
those threads then cannot be reopened to write in and those items stop mirroring until it is put
back.

A repository registered before that check existed never passed it, and the permission can be
revoked at any time, so a close that is refused says so in the thread rather than only in the log.

On the very first start the commands are registered globally, and Discord serves those from a cache
that can take up to an hour to catch up. The log says so. Until it does, typing `/` shows nothing
and there is nothing wrong: wait, or restart the Discord client, which usually shortens it.

**Then, in the server, in this order.**

```text
/register <github_repo_link>          binds the repository, PR threads land in this channel
/set_channel issues #channel          where issue threads go
/set_channel project tickets #channel only if a board is being mirrored
/link <github_username> @member        once per person, so pings become mentions
/link_team <team> @role               so a review asked of a team reaches somebody
```

Only `/register` has to come first. Issues fall back to the pull request channel until they are
given one of their own; project tickets do not, so a board stays unmirrored until `/set_channel`
names a channel for them.

The role given to `/link_team` needs **Allow Anyone To @mention This Role** turned on in its
settings. Discord notifies a role's members only when that is set or the sender holds Mention
@everyone, @here, and All Roles, and roles are created without it, so otherwise the ping shows in
the thread as a blue pill and reaches nobody. `/link_team` says so when it sees it. The other way
is to add that permission to the invite; it is safe here because the bot refuses to resolve
`@everyone` in anything it sends, whoever wrote the text.

A forum channel set to **Require Tags** is refused by `/register` and `/set_channel`, because
nothing here picks a tag and Discord rejects every post without one.

**Pointing a kind somewhere new takes its threads with it.** Discord cannot move a thread between
channels, so each item gets a replacement in the new one, and the thread it leaves gains a line
linking to that replacement and is then locked and archived. The mirrored comments stay readable
where they were. Ten threads move per run and the reply says how many are left, so a long backlog
is a few runs of the same command; running it again with the same channel is how you carry on.

That is the fix for registering in the wrong channel, which used to be permanent: the mapping was
corrected, new items landed correctly, and every item that already had a thread went on being
written to in the wrong place for ever.

## Running it on a server

`compose.prod.yaml` and `Caddyfile` are the server stack. Three things they do that the laptop
stack does not:

- Pull the image CI built instead of building on the box, and publish no port but Caddy's, so the
  database and the app are only reachable across the compose network.
- Terminate TLS, and 404 `/docs`, `/redoc` and `/openapi.json`, which FastAPI otherwise serves to
  anyone.
- Run `alembic upgrade head` to completion before the app starts. Nothing else would catch a
  deploy that skipped it: the startup check proves the database was migrated at all, not that it
  reached the revision this code expects.

Point an A record at the machine first, or Caddy cannot get a certificate. Then, on the machine,
write `.env` with `SHANNON_HOSTNAME`, `ACME_EMAIL`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and the
three `SHANNON_*` secrets, and:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <user> --password-stdin   # classic PAT, read:packages
docker compose -f compose.prod.yaml up -d
```

Point the GitHub webhook at `https://<hostname>/webhooks/github`.

Upgrades go through `scripts/deploy.sh`, from a laptop, one command and one ssh password:

```bash
ssh root@<host> /opt/shannon/scripts/deploy.sh              # head of main
ssh root@<host> /opt/shannon/scripts/deploy.sh <commit>     # a named commit
ssh root@<host> /opt/shannon/scripts/deploy.sh --rollback   # the previous build
```

It pins the image to `sha-<commit>` rather than following a moving tag, so what is running is a
fact rather than whatever `edge` pointed at that afternoon, and it refuses outright if CI never
published an image for the commit you asked for. `scripts/README.md` is the whole story.

**It does not decide when to run; a person does.** A pull loop restarts a live bot on a green
merge with nobody watching, and a merge on a Friday evening is exactly when nobody is. The cost of
that choice is that the box sits behind `main` until somebody deploys, which is what the hourly
`Deployed` workflow opens an issue about. That issue is the reminder; it is not a failure.

`curl https://<hostname>/health` gives back the commit that is actually running, which is the
quick way to tell a broken change from an undeployed one. That has cost real time: a merged change
was read as a bug in the bot for an afternoon.

Only one copy may run at a time: two would both hold the Discord gateway and both lease from the
queue. The Postgres credentials are read once, when the volume is created.

A deploy announces itself in Discord through a webhook and rolls the image back if the new
container never reports healthy. It cannot roll the database back, which `scripts/README.md` is
blunt about: the migration has been applied and `alembic downgrade` is not run.

### One bot, several servers

One instance serves as many Discord servers as it is invited to. Commands register globally for
that reason, and every table that holds a decision a server made is keyed by guild: which
repository it mirrors, which channel each kind of item threads into, who is linked to whom, who
asked not to be pinged. A webhook resolves to a server through the repository it came from, never
through a configured guild id.

What is shared, and what that costs:

- **One `SHANNON_GITHUB_TOKEN`** has to see every repository, with write access to issues in each,
  because every `/set_*` puts a label on the item. GitHub's 5,000 an hour is per token, so it is
  now shared across all of them.
- **One `SHANNON_GITHUB_WEBHOOK_SECRET`.** Every repository's webhook is configured with the same
  one; there is no per-repository secret to verify against.
- **One set of role names.** `SHANNON_ROLE_*` are read once at startup and apply everywhere, so a
  server that calls its managers something else grants nothing to anybody but guild
  administrators. This is the one that surprises people.
- **One board, or none.** `SHANNON_GITHUB_PROJECT_NUMBER` names a single project, and nothing
  elects which server's board it belongs to. With more than one server registered the board mirror
  stops itself and `/health` reports `poller: false` until the number goes back to zero and the
  process restarts. Leave it at `0` unless exactly one server is registered.

Adding one: invite the bot with the `bot` and `applications.commands` scopes and the permissions
above, point that repository's webhook at the same URL with the same secret, then `/register` and
`/set_channel` in the new server. A global command takes up to an hour to appear the first time,
which looks exactly like a broken deploy and is not.

The two limits that do not move: one repository per server, and one server per repository. There
is no supported way to unregister either.

## Configuration

Read from the environment with a `SHANNON_` prefix, or from `.env`. Everything has a default and
nothing is required to construct, so a misconfigured deployment starts and fails later rather than
at the door.

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_DATABASE_URL` | `postgresql+asyncpg://shannon:shannon@localhost:5433/shannon` | The default is the compose database |
| `SHANNON_GITHUB_WEBHOOK_SECRET` | empty | HMAC secret. Empty answers 500 to every delivery rather than waving them through |
| `SHANNON_DISCORD_TOKEN` | empty | Bot token. Empty runs without the gateway |
| `SHANNON_GITHUB_TOKEN` | empty | REST token. Needs **write** access to issues: `/register`, `/pr`, `/issue` and `/refresh` only read, but every `/set_*` command and every board move puts a label on the item |
| `SHANNON_ROLE_ADMIN` | `Admin` | Role names per tier, comma separated for more than one |
| `SHANNON_ROLE_PROJECT_MANAGER` | `Project Manager` | |
| `SHANNON_ROLE_REVIEWER` | `Reviewer` | Grants no command today. Deciding a change is good and recording that the project has accepted it are different jobs, and only the second is written down here |
| `SHANNON_ROLE_DEVELOPER` | `Developer` | |
| `SHANNON_API_HOST` | `0.0.0.0` | |
| `SHANNON_API_PORT` | `8000` | |
| `SHANNON_LOG_LEVEL` | `INFO` | Uppercased, not validated |
| `SHANNON_BUILD` | `unknown` | The commit the image was built from, reported by `/health`. Written by the Dockerfile, so do not set it: compose passes `.env` into the container and it would override the real one. It is the only setting missing from `.env.example`, for that reason |
| `SHANNON_GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise |
| `SHANNON_GITHUB_TIMEOUT_SECONDS` | `10.0` | |
| `SHANNON_GITHUB_PROJECT_NUMBER` | `0` | The project board to mirror, by the number in its URL. Zero means none |
| `SHANNON_PROJECT_POLL_SECONDS` | `60.0` | How often that board is read |
| `SHANNON_BOARD_MAY_SET_STATUS` | `false` | Whether dragging a card may change the item's status. Off, because nothing GitHub sends says who moved a card, so a board that could move items would be a way past the Project Manager role below |
| `SHANNON_WORKER_POLL_SECONDS` | `2.0` | How often an empty queue is checked |
| `SHANNON_WORKER_BATCH_SIZE` | `10` | |
| `SHANNON_WORKER_MAX_ATTEMPTS` | `16` | Roughly two hours of backoff before a delivery is dropped |
| `SHANNON_WORKER_MAX_BACKOFF_SECONDS` | `900.0` | Cap on the doubling delay |
| `SHANNON_WORKER_LEASE_SECONDS` | `900.0` | How long a leased delivery is held |
| `SHANNON_WORKER_DELIVERY_TIMEOUT_SECONDS` | `60.0` | Deadline on one delivery |
| `SHANNON_WORKER_SHUTDOWN_GRACE_SECONDS` | `5.0` | |
| `SHANNON_DELIVERY_RETENTION_DAYS` | `7` | How long finished deliveries and their payloads are kept |

A project board is read on a timer rather than delivered. GitHub sends `projects_v2` webhooks
for organisation projects only and none at all for a personal account's, and the `project_card`
events the requirements name belong to Projects (classic), which was sunset in August 2024. The
token needs project read access, and the board's tickets need a channel: they have no fallback,
so `/set_channel project tickets` is what turns the mirror on.

One rule spans fields: `worker_lease_seconds` must cover `worker_batch_size *
worker_delivery_timeout_seconds`, or construction fails. A lease expiring mid-batch would let a
second worker take deliveries this one is still on.

`webhook_events.payload` holds private repository content: titles, comment bodies, author names.
Retention bounds it and the payload goes with the row.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/register <github_repo_link>` | Admin, Project Manager | Binds a repository to this server and points PR threads at the current channel. One repository per server, and no way to undo it |
| `/set_channel <object_type> <channel>` | Admin, Project Manager | Where threads of one kind appear, and where the ones already open are moved to. Ten per run; the reply says how many are left |
| `/pr <pr_link>` | Developer, Project Manager | Fetches a pull request and mirrors it |
| `/issue <issue_link>` | Developer, Project Manager | Fetches an issue and mirrors it |
| `/refresh [only]` | Developer, Project Manager | Opens a thread for every open pull request and issue that has no thread yet, leaving the ones that do alone. Nobody is pinged: a backlog is not news. Twenty-five per run, and the reply says how many are left |
| `/link <github_username> [member]` | Admin, Project Manager | Connects a GitHub login to a Discord account so pings become mentions. The login is checked against GitHub, because one that does not exist is recorded happily and then silently reaches nobody |
| `/link_team <github_team> <role>` | Admin, Project Manager | Points a Discord role at a GitHub team, so a review asked of that team pings the role |
| `/mentions [state]` | Anyone | Whether this bot's messages notify you in this server. Off still names you on every item you are on, as a mention Discord shows and does not ring. With no argument it says which way round you are |
| `/set_backlog` `/set_not_reviewed` `/set_in_review` `/set_ready_for_merge` `/set_done` | Project Manager | Moves the item whose thread you are in. `/set_done` shuts the thread, and a pull request has to be ready for merge first |
| `/set_high_priority` `/set_med_priority` `/set_low_priority` | Project Manager | Same, for priority |

Guild only, replies always ephemeral. Role names are configured strings, matched case
insensitively, so renaming a Discord role revokes the tier until the setting catches up. Holding
several roles grants the union of what each allows, and a guild administrator passes every gate
whatever the configuration says.

`/mentions` is the one command with no role behind it. Every other one decides something about
the server; that one decides whether your own name notifies you, and asking a project manager to
turn off your own pings is a request nobody makes twice. It reaches everything this bot writes
except a role: `@org/team` pings the whole Discord role and Discord gives nobody a way to leave
one person out of one.

Linking is a project manager's job, both halves of it. Claiming your own account used to be
ungated, on the reasoning that it is yours to claim, and nothing checked that it was: GitHub is
never asked, so anybody could take any login and receive every mention meant for it in this server.

The eight workflow commands take no argument and act on the thread they are run in, which is the
item you are looking at. Status and priority live as labels on the repository, and each is single
valued: setting one takes the previous one off, in whatever spelling the repository was using.

## Architecture

Nothing below `container.py` builds its own collaborators, and everything crossing the network
sits behind a protocol, so the services layer runs in tests with only Discord and GitHub replaced.

```text
shannon/
  domain/       enums, snapshots, errors, timezone helpers. Imports nothing else
  db/           models, session factory, one store per table
  github/       REST client, URL parsing, signature check, payload parsers
  discord_bot/  gateway client, thread gateway, permission gate, rendering, text safety
  services/     sync/      one item into its thread: policies, staleness, threads,
                           notifications, and the same job driven by a command
                delivery/  the queue and the worker that drains it
                notes, reviews, channels, linking, registration
  api/          FastAPI app, webhook and health routes
  commands/     the slash commands, which drive services the way the routes do
  runtime/      liveness, task supervision, startup and shutdown
  config.py     settings
  container.py  the wiring
  main.py       assembles the app and hands it to uvicorn
```

Listed bottom up, and imports only ever run down that list. `commands/` sits beside `api/`
rather than inside `discord_bot/` for that reason: it drives services in response to a person,
which makes it a delivery mechanism and not an adapter. The bot is handed its error translator
instead of importing one, which is what keeps the adapter layer from reaching upward.

Bot and API share a process. The worker waits for the gateway before its first batch, since a
delivery attempted before Discord connects only burns an attempt. Shutdown stops the worker, hands
its unstarted batch back, closes the gateway and disposes the pool, reporting any step that fails
rather than abandoning the rest.

## Data model

| Table | Holds |
| --- | --- |
| `repositories` | One per registered repository. Unique on guild and on GitHub id, so a webhook resolves to exactly one server |
| `channel_mappings` | Which channel a kind of item threads into |
| `tracked_items` | One per mirrored PR or issue: thread, message, state, and the high water mark that orders deliveries |
| `item_assignments` | Who is on an item and in what capacity. `notified_at` is the ping claim, `fulfilled_at` closes a review request |
| `mirrored_notes` | Comments and reviews already posted, claimed before posting so a retry cannot repeat one |
| `webhook_events` | The queue: payload, status, attempts, backoff, lease, last error |
| `user_links` | GitHub login to Discord account, per server |
| `muted_members` | Who asked not to be notified, per server. A row is the whole of the fact, so no row means pinged |
| `team_links` | GitHub team slug to Discord role, per server. Kept apart from `user_links` because a slug and a login are separate namespaces on GitHub and only one of them is claimable here |

Enums are `VARCHAR`, not native PostgreSQL types, so adding a status needs no `ALTER TYPE`. Worth
knowing that they are unconstrained in the database: the mapping asks for a `CHECK` and SQLAlchemy
does not emit one, so the column accepts any string that fits and the application is the only
thing enforcing the values.

Alembic revisions `0001` to `0017`. A test applies them to an empty database and diffs the result
against the models, so the two cannot drift apart, and another compares this section against what
is on disk, because both the range and the table above had already gone stale once.

Nothing prunes except `webhook_events`. `mirrored_notes` grows by one row per comment and review
and has no cleanup path.

## HTTP surface

| Route | Answers |
| --- | --- |
| `POST /webhooks/github` | 200 with `accepted`, `duplicate` or `ignored`. 400 for a missing header or unusable body, 401 for a bad signature, 413 past the 25MB cap, 500 if the secret is unset |
| `GET /health` | `database`, `worker`, `bot` and `poller` as booleans, `version` as the commit answering, 503 if any of the first three is false |

`version` is the commit the image was built from. It is there because a change that was merged
and never deployed and a change that does not work look identical from outside, and telling them
apart used to mean getting onto the box. `unknown` means an image built by hand rather than by CI.
It is served to anyone, and against a public repository that says which fixes this deployment has
and which it has not; `Caddyfile` says why that is accepted.

`/health` reports what the process is doing rather than that it is listening. A dead worker or a
dropped gateway leaves the endpoint accepting deliveries nothing will act on, which is worth a
restart. The database probe is cached for a few seconds so the public endpoint cannot exhaust the
pool the worker runs on.

`poller` is reported without being counted, and it is the only one that is. This process still
does its job without a board: webhooks arrive, threads are written, and only board movement stops,
so failing the check would restart a working process and throw away whatever the worker had in
hand. It is reported because the poller is the one task with nothing wired to stop the process
when it dies, so without this it goes with a line in the log and everything after that answers
that all is well. It is true where no board is configured, which is the default.

`/docs`, `/redoc` and `/openapi.json` are served unconditionally, `/health` is unauthenticated,
and there is no middleware of any kind.

## Known limitations

Four things the bot is known to get wrong. All are narrow, and all are written down here rather
than fixed. Only the second leaves anything lost: the comment it drops is never mirrored
afterwards.

**A review handled before the request it answers.** GitHub sends the review request and the
review as separate deliveries, and nothing guarantees the order they are handled in: a delivery
that fails once backs off, and the one behind it goes first. Handle the review first and there
is no request row for it to close, so the request written a moment later reads as outstanding
and the reviewer is pinged for a review they have already given. What is wrong is the ping, not
the state. The row closes the next time that person reviews, and a request GitHub has dropped is
deleted on the next ordinary event for the pull request. The fix is for a review to write down
that it happened even when the row it answers is not there yet, and that is left for later.

**A comment on an item that has never been tracked.** A comment is not an item event and carries
nothing to build a thread from, so where the repository is registered but the item has no row,
the note is logged and dropped, and that comment is not mirrored later either. It is not the
same as a thread that has been deleted, which is rebuilt: that item is tracked, and the sync has
a number to read GitHub with. It happens for an item that was already open before the repository
was registered, or one whose events all landed while the bot was down, and it stops the moment
any item event for that number arrives and builds the thread. The hook that rebuilds a deleted
thread could serve this case as well; what has kept it out is that every comment on anything
untracked would then cost a call to GitHub, for a case that is mostly a first run. It may be
fixed later.

**A status label moved on GitHub, against a status the block still shows.** The five statuses live
as labels on the repository, but nothing on the webhook path reads one back onto the item: only
the slash commands and the board write that column. So labelling an item `BACKLOG` in GitHub posts
a line saying so while the block above goes on reading `IN_REVIEW`, and both are true of different
things. The line reports what somebody did to the labels, which is what every line in a thread
reports. Making a GitHub label move the stored status is a decision about which of GitHub and
Discord wins, and it is a bigger question than the line that made it visible.

**A tag resolved from a comment body cannot be checked against the account that wrote it.** Every
other mention the bot builds is checked against the GitHub id the payload carries, so a login
somebody freed and a stranger took does not inherit the previous holder's mention. A name read out
of a comment body carries no id at all, so that check cannot run and the name alone decides. It
follows the rule the link table already states for a payload with no id: no evidence is not
evidence, and refusing on it would take away mentions that work.

## License

Apache-2.0. See [LICENSE](LICENSE).
