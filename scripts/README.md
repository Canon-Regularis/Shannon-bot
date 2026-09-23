# Deploying

Two pieces, and the split between them is the whole design.

`deploy.sh` is the runbook as one command. It does not decide when to run; a person does.
`.github/workflows/deployed.yml` runs hourly, asks `/health` which commit is answering, compares
it to the head of `main`, and opens an issue when they differ. The trigger stays human; the
procedure and the noticing do not.

That split is the answer to the objection this project already wrote down once. A pull loop or a
push-to-deploy job restarts a live bot on a green merge with nobody watching, and a restart is not
free here: a worker killed mid-batch leaves up to nine deliveries leased in `PROCESSING` until
`worker_lease_seconds` (900) lapses, which is up to fifteen minutes of mirroring that silently
does not happen. The reason not to automate the trigger was never that deploying is hard. It was
that nobody would notice the deploy that did not happen. `/health` reporting the commit made that
checkable, so it is checked, and the trigger can stay where it belongs.

Nothing here holds a credential to the server. No SSH key, no password, no registry token is
stored in GitHub. The workflow reads one public unauthenticated endpoint and writes an issue.

## One-time setup

### On the server, as root

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

`/opt/shannon` becomes a clone in place. Nothing moves and nothing stops: `.env` is gitignored so
checkout leaves it alone, and the named volumes are keyed on the compose project name, which comes
from the directory name. **Leave the directory called `shannon`.** Rename it and compose starts an
empty Postgres beside the real one; `deploy.sh` refuses to run rather than let that happen.

`deploy.sh` arrives executable, because git carries the bit. A `chmod +x scripts/deploy.sh` used
to stand in the block above, and it was the line that eventually stopped a deploy: the script was
committed non-executable, so the chmod was a real modification to a tracked file and it sat in the
clone for as long as the box existed. git will not overwrite a dirty file, so the first deploy that
changed `deploy.sh` aborted at the checkout and said so in git's words rather than anybody's. Do
not put it back.

Making it a clone fixes a gap the manual process had: `compose.prod.yaml` and `Caddyfile` are
versioned with the code, and `pull && up -d` never updated them. They were whatever was copied
onto the box the day it was set up.

The first run writes `SHANNON_IMAGE_TAG=sha-<commit>` into `.env`, pinning the box to one
immutable build instead of the moving `edge` tag. From then on a bare `docker compose up -d`, or a
reboot, brings back the same build rather than whatever `edge` moved to overnight.

### On GitHub, once

Add one repository secret:

| Secret | Value |
| --- | --- |
| `SHANNON_HEALTH_URL` | `https://<hostname>/health` |

A secret rather than a variable so it is masked in this public repository's run logs. It protects
very little: the hostname is in a certificate transparency log and the endpoint is unauthenticated
by design. It keeps the address of the box out of public logs and public issue bodies, which is
worth one setup step. The `deployment` label the issues carry is created by the workflow itself,
so there is nothing else to do.

Red in that workflow means the bot is not serving: `/health` did not answer, or answered 503.
Green means it answered 200 and healthy. Whether it is running the head of `main` is said in the
issue, not in the colour.

Drift used to be red as well. It stopped being, because this deploy runs when a person decides and
the box is normally behind `main` until they do, so red said two different things and neither
could be acted on without reading which. **The issue is now the only signal for drift**, so watch
the repository or the `deployment` label; an unwatched issue is no signal at all. Actions failure
email (Settings → Notifications) still covers the two states that stayed red.

## Deploying

From the laptop, one command and one ssh password:

```bash
ssh root@<host> /opt/shannon/scripts/deploy.sh              # head of main
ssh root@<host> /opt/shannon/scripts/deploy.sh <commit>     # a named commit
ssh root@<host> /opt/shannon/scripts/deploy.sh --rollback   # the previous build
```

In order, it:

1. Reads `/health` and records the commit that is running, and whether it is healthy.
2. Resolves the target commit and stops if that is already what is running.
3. Asks the registry whether `sha-<commit>` exists. **This is the check that pays for itself.**
   No image means CI has not finished, CI was red and published nothing, or the GHCR token has
   expired. All three used to look like "the deploy did not work"; none is a reason to touch a
   running bot, and nothing has been changed when it stops here.
4. Checks out that commit, so `compose.prod.yaml`, the `Caddyfile` and this script are all the
   target commit's, then hands over to a copy of itself rather than reading on past its own edit.
5. Pulls. A pull that fails has cost nothing.
6. Waits up to two minutes for `webhook_events` to have nothing in `PROCESSING` with a live lease,
   so the restart strands no deliveries. Refuses to go on if it does not go quiet, unless
   `--force`.
7. Pins the tag, then runs `alembic upgrade head` as its own step rather than as a dependency of
   `up`. A migration that fails is one line, the `.env` tag goes back, and the old container is
   still serving.
8. `up -d`.
9. Polls `/health` through the public URL for three minutes, and only calls it done when the
   version matches the target commit **and** `healthy` is true. The public URL, so Caddy and the
   certificate are proved too.
10. On failure: saves the app logs to `/var/log/shannon-failed-<commit>.log`, prints the last
    forty lines, puts the previous image back and verifies that.

`--force` skips the quiet-queue wait and redeploys a commit already running. `--no-wait` skips
only the wait.

## What it cannot do

**It cannot roll the database back.** A rollback restores the image and nothing else. The
migration has been applied and `alembic downgrade` is not run, because a downgrade can drop a
column and the data in it. The old code then runs against a newer schema, which is fine for an
additive migration and is not fine for one that removed or renamed something the old code reads.
The script says this in as many words when it rolls back, because it is the moment somebody needs
to read it.

**It cannot watch a second box.** The workflow probes one `SHANNON_HEALTH_URL`, so a second
deployment is not monitored by it and its drift is nobody's alert.

**It cannot roll back to nothing.** A first install, or a deploy attempted while the bot was
already down, has no known-good commit to return to. It stops, leaves the stack as it is so the
logs are live, and says so.

**It cannot deploy without a person.** That is the design, not an omission.
