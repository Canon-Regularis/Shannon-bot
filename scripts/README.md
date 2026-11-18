# Deploying

Deploying has two pieces:

| Piece | What it does |
| --- | --- |
| `deploy.sh` | Runs the whole deploy runbook as one command. A person decides when to run it. |
| `.github/workflows/deployed.yml` | Runs every hour. It asks `/health` which commit is answering and compares it to the head of `main`. When they differ, it opens an issue. |

A person starts the deploy. The script does the steps, and the workflow notices when a deploy is
missing.

### Why the trigger stays manual

This project has already decided against a pull loop or a push-to-deploy job. Either one restarts
the live bot after every green merge, with nobody watching. A restart has a cost here. A delivery
is one queued event for the bot to post to Discord. The worker takes deliveries in batches and
holds a lease on each one: a lock that marks it `PROCESSING`. A worker killed mid-batch leaves up
to nine deliveries leased until `worker_lease_seconds` (900) runs out. Those deliveries are not
posted for up to fifteen minutes, and nothing says so.

Deploying is not hard. The risk is that nobody notices a deploy that did not happen. `/health`
reports the commit, so the workflow can check it, and the trigger stays with a person.

### No credentials in GitHub

Nothing here holds a credential to the server. GitHub stores no SSH key, no password and no
registry token. The workflow reads one public, unauthenticated endpoint and writes an issue.

## One-time setup

### On the server, as root

These commands turn `/opt/shannon` into a git clone of this repository, then run the first
deploy.

```bash
apt-get update && apt-get install -y git jq curl

cd /opt/shannon
git init -q
git remote add origin https://github.com/Canon-Regularis/Shannon-bot.git
git fetch origin

# Refuses if a file on the box differs from the repo, and names it. Diff anything it lists and
# decide before going on; --force after that discards the box's copy.
git checkout --detach origin/main

./scripts/deploy.sh
```

`/opt/shannon` becomes a clone in place. Nothing moves and nothing stops:

- `.env` is gitignored, so the checkout leaves it alone.
- The named volumes are keyed on the compose project name. Compose takes that name from the
  directory name.

**Leave the directory called `shannon`.** If you rename it, compose starts an empty Postgres beside
the real one. `deploy.sh` refuses to run rather than let that happen.

**Do not add `chmod +x scripts/deploy.sh` to the steps.** git carries the executable bit, so
`deploy.sh` arrives executable. The steps used to include that `chmod` while the script was
committed non-executable. The `chmod` was then a change to a tracked file, and it stayed in the
clone for as long as the box existed. git does not overwrite a changed file, so the first deploy
that changed `deploy.sh` stopped at the checkout with only git's own error message.

The clone also fixes a gap in the old manual process. `compose.prod.yaml` and `Caddyfile` are
versioned with the code, but `pull && up -d` never updated them. They stayed as copied onto the
box on the day it was set up.

The first run writes `SHANNON_IMAGE_TAG=sha-<commit>` into `.env`. This pins the box to one
build that never changes, instead of the moving `edge` tag. After that, a bare
`docker compose up -d`, or a reboot, brings back the same build, not whatever `edge` moved to
overnight.

### On GitHub, once

Add one repository secret:

| Secret | Value |
| --- | --- |
| `SHANNON_HEALTH_URL` | `https://<hostname>/health` |

It is a secret, not a variable, so that it is masked in this public repository's run logs. That
protects very little: the hostname is in a certificate transparency log, and the endpoint has no
authentication by design. It does keep the address of the box out of public logs and public issue
bodies, which is worth one setup step.

`.github/workflows/deployed.yml` creates the `deployment` label its issues carry, so there is
nothing else to set up. What its runs mean:

| Run | Meaning |
| --- | --- |
| Red | The bot is not serving: `/health` did not answer, or answered 503. |
| Green | `/health` answered 200 and healthy. |

The colour does not say whether the box runs the head of `main`. The issue says that.

"Drift" means the box runs an older commit than the head of `main`. Drift used to turn the run red
as well. It no longer does: a person decides when to deploy, so the box is normally behind `main`
until they do. Red then meant two different things, and you had to read which before you could
act.

**The issue is now the only signal for drift.** Watch the repository or the `deployment` label; an
unwatched issue is no signal at all. Actions failure email (Settings → Notifications) still covers
the two states that stay red.

## Deploying

Run it from your laptop. It is one command, and ssh asks for one password:

```bash
ssh root@<host> /opt/shannon/scripts/deploy.sh              # head of main
ssh root@<host> /opt/shannon/scripts/deploy.sh <commit>     # a named commit
ssh root@<host> /opt/shannon/scripts/deploy.sh --rollback   # the previous build
```

| Option | What it does |
| --- | --- |
| `--force` | Redeploys a commit that is already running and healthy. If deliveries are still leased after the two-minute wait (step 6), it carries on instead of refusing. |
| `--no-wait` | Skips the wait in step 6 altogether. |

A delivery is one GitHub webhook event, stored as a row in `webhook_events`. While the worker
handles a delivery it holds a lease on it: the row is marked `PROCESSING` and no other worker
takes it until the lease runs out.

The script does these steps in order:

1. Reads `/health`. It records the commit that is running and whether it is healthy.
2. Resolves the target commit. It stops if that commit is already running and healthy, unless
   `--force`.
3. Asks the registry whether the image `sha-<commit>` exists. **This check prevents the most
   common failure.** No image means one of three things: CI has not finished, CI was red and
   published nothing, or the GHCR token has expired. Before this check, all three looked like "the
   deploy did not work". None of them is a reason to touch a running bot. When the script stops
   here, it has changed nothing.
4. Checks out the target commit, so `compose.prod.yaml`, the `Caddyfile` and this script all come
   from that commit. Then it hands over to a copy of itself, so it does not read on past its own
   edited file.
5. Pulls the image. If the pull fails, nothing has changed yet.
6. Waits up to two minutes until `webhook_events` has nothing in `PROCESSING` with a live lease.
   This way the restart strands no deliveries. If the queue does not go quiet, it refuses to go
   on, unless `--force`.
7. Pins the tag in `.env`, then runs `alembic upgrade head` as its own step, not as a dependency
   of `up`. If the migration fails, the script says so in one line, puts the `.env` tag back, and
   the old container keeps serving.
8. Runs `up -d`, then recreates the `caddy` container on every deploy. A checkout replaces the
   Caddyfile, and Docker still holds the old one, so recreating is what makes the target commit's
   Caddyfile live. A Caddyfile edited by hand on the box also goes live at this point.
9. Polls `/health` through the public URL for three minutes. It calls the deploy done only when
   the version matches the target commit **and** `healthy` is true. It uses the public URL so
   that Caddy and the certificate are checked too. It then requests `/oauth/github/callback`. If
   that answers 404 (Caddy's catch-all), the deploy still counts as done, with a warning that every
   one-time link is broken. It does not roll back.
10. On failure, it saves the app logs to `/var/log/shannon-failed-<commit>.log`, prints the last
    forty lines, puts the previous image back and checks that the previous image is healthy.

## What it cannot do

**It cannot roll the database back.** A rollback restores the image and nothing else. The
migration stays applied. The script does not run `alembic downgrade`, because a downgrade can drop
a column and the data in it. The old code then runs against a newer schema. That is fine for an
additive migration. It is not fine for a migration that removed or renamed something the old code
reads. The script prints this warning when it rolls back, because that is when somebody needs to
read it.

**It cannot watch a second box.** The workflow checks one `SHANNON_HEALTH_URL`. It does not
monitor a second deployment, so nobody is told when that one falls behind `main`.

**It cannot roll back when nothing ran before.** A first install, or a deploy started while the bot
was already down, has no known-good commit to return to. The script stops, leaves the stack as it
is so the logs you read are the live ones, and says so.

**It cannot deploy without a person.** This is on purpose: a person always starts the deploy.
