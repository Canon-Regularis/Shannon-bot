# shannon

GitHub repository activity, mirrored into Discord threads

**shannon** binds one GitHub repository to one Discord server. Every pull request and issue gets a
thread, kept in step as the item changes, with comments and reviews underneath it. Webhooks are
answered as soon as they are checked and written down. Everything slow runs behind a queue, since
GitHub allows ten seconds and never redelivers anything it recorded as failed.

## What it does

- **Threads.** One per item, opened on the first event and edited in place after. The metadata
  block carries name, type, state, link, author, assignees, reviewers, status, priority, tags and
  last updated.
- **Comments and reviews.** Quoted into the item's thread with a link back. Edits and deletions
  are not mirrored, so a thread records what was said at the time.
- **Pings.** Reviewers and assignees are told once each, as mentions where the account is linked.
  The claim is taken before the message goes out and handed back if it fails.
- **Manual sync.** `/pr` and `/issue` pull an item from the REST API, for whatever the webhooks
  missed.
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
docker compose up -d db             # PostgreSQL 17 on localhost:5433
cp .env.example .env                # set the webhook secret and the bot token
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

`Manage Threads` fails later still. Everything works until an issue closes or somebody runs
`/set_done`, and then the lock is refused; it is a separate permission from the ones that open and
write to a thread.

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

## Configuration

Read from the environment with a `SHANNON_` prefix, or from `.env`. Everything has a default and
nothing is required to construct, so a misconfigured deployment starts and fails later rather than
at the door.

| Setting | Default | Controls |
| --- | --- | --- |
| `SHANNON_DATABASE_URL` | `postgresql+asyncpg://shannon:shannon@localhost:5433/shannon` | The default is the compose database |
| `SHANNON_GITHUB_WEBHOOK_SECRET` | empty | HMAC secret. Empty answers 500 to every delivery rather than waving them through |
| `SHANNON_DISCORD_TOKEN` | empty | Bot token. Empty runs without the gateway |
| `SHANNON_GITHUB_TOKEN` | empty | REST token. Needs **write** access to issues: `/register`, `/pr` and `/issue` only read, but every `/set_*` command and every board move puts a label on the item |
| `SHANNON_ROLE_ADMIN` | `Admin` | Role names per tier, comma separated for more than one |
| `SHANNON_ROLE_PROJECT_MANAGER` | `Project Manager` | |
| `SHANNON_ROLE_REVIEWER` | `Reviewer` | |
| `SHANNON_ROLE_DEVELOPER` | `Developer` | |
| `SHANNON_API_HOST` | `0.0.0.0` | |
| `SHANNON_API_PORT` | `8000` | |
| `SHANNON_LOG_LEVEL` | `INFO` | Uppercased, not validated |
| `SHANNON_GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise |
| `SHANNON_GITHUB_TIMEOUT_SECONDS` | `10.0` | |
| `SHANNON_GITHUB_PROJECT_NUMBER` | `0` | The project board to mirror, by the number in its URL. Zero means none |
| `SHANNON_PROJECT_POLL_SECONDS` | `60.0` | How often that board is read |
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
| `/set_channel <object_type> <channel>` | Admin, Project Manager | Where threads of one kind appear |
| `/pr <pr_link>` | Developer, Project Manager | Fetches a pull request and mirrors it |
| `/issue <issue_link>` | Developer, Project Manager | Fetches an issue and mirrors it |
| `/link <github_username> [member]` | Admin, Project Manager | Connects a GitHub login to a Discord account so pings become mentions. The login is checked against GitHub, because one that does not exist is recorded happily and then silently reaches nobody |
| `/link_team <github_team> <role>` | Admin, Project Manager | Points a Discord role at a GitHub team, so a review asked of that team pings the role |
| `/set_backlog` `/set_not_reviewed` `/set_in_review` `/set_ready_for_merge` `/set_done` | Reviewer, Project Manager | Moves the item whose thread you are in. `/set_done` locks the thread, and a pull request has to be ready for merge first |
| `/set_high_priority` `/set_med_priority` `/set_low_priority` | Reviewer, Project Manager | Same, for priority |

Guild only, replies always ephemeral. Role names are configured strings, matched case
insensitively, so renaming a Discord role revokes the tier until the setting catches up. Holding
several roles grants the union of what each allows, and a guild administrator passes every gate
whatever the configuration says.

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
| `team_links` | GitHub team slug to Discord role, per server. Kept apart from `user_links` because a slug and a login are separate namespaces on GitHub and only one of them is claimable here |

Enums are `VARCHAR`, not native PostgreSQL types, so adding a status needs no `ALTER TYPE`. Worth
knowing that they are unconstrained in the database: the mapping asks for a `CHECK` and SQLAlchemy
does not emit one, so the column accepts any string that fits and the application is the only
thing enforcing the values.

Alembic revisions `0001` to `0011`. A test applies them to an empty database and diffs the result
against the models, so the two cannot drift apart, and another compares this section against what
is on disk, because both the range and the table above had already gone stale once.

Nothing prunes except `webhook_events`. `mirrored_notes` grows by one row per comment and review
and has no cleanup path.

## HTTP surface

| Route | Answers |
| --- | --- |
| `POST /webhooks/github` | 200 with `accepted`, `duplicate` or `ignored`. 400 for a missing header or unusable body, 401 for a bad signature, 413 past the 25MB cap, 500 if the secret is unset |
| `GET /health` | `database`, `worker` and `bot` as booleans, 503 if any is false |

`/health` reports what the process is doing rather than that it is listening. A dead worker or a
dropped gateway leaves the endpoint accepting deliveries nothing will act on, which is worth a
restart. The database probe is cached for a few seconds so the public endpoint cannot exhaust the
pool the worker runs on.

`/docs`, `/redoc` and `/openapi.json` are served unconditionally, `/health` is unauthenticated,
and there is no middleware of any kind.

## License

Apache-2.0. See [LICENSE](LICENSE).
