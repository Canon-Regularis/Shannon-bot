# Deploying without typing anything

The box pulls. Nothing pushes to it.

A systemd timer on the server asks ghcr.io every five minutes whether the tag it follows points
at a different image from the one the app container is running. Almost always the answer is no
and the run ends having downloaded nothing and written nothing. When the answer is yes it pulls,
runs the migration, replaces the container, waits for `/health`, and says so in Discord.

## Why this shape

**Nothing holds a key to this machine.** The alternative designs all end with a deploy credential
living in GitHub: an SSH key, or a token for a webhook receiver. This repository is public, the
server takes root logins over a password, and there is no key auth on it. A deploy secret in
Actions would be the most valuable thing in the project and it would sit in the one place a
misconfigured workflow can read. Here there is no such secret, because there is no inbound path:
the server's firewall does not have to open, `GITHUB_TOKEN` does not have to grow a scope, and a
compromised workflow can publish a bad image but cannot reach the box.

**`edge` is not what it follows.** `SHANNON_IMAGE_TAG=stable` in `/opt/shannon/.env`, and
`release.yml` only moves `stable` on a `v*` tag. So a green push to main builds, tests and
publishes an image and deploys nothing. Pushing `v0.6.0` deploys. The project decided against
auto-update once on the grounds that "a green push to main restarting a live bot with no one
watching" was not worth it, and that objection is correct about `edge` and does not apply to a
tag somebody types on purpose.

Following `edge` is still supported. Set it and every green merge lands on the server within five
minutes. That is a real choice and it is written out in `/opt/shannon/.env` rather than left to
the compose file's default, so that whoever made it made it.

**One copy, always.** `docker compose up -d` stops the old container before it starts the new one.
Nothing here scales, nothing rolls, and nothing runs two app containers for a moment. Two would
both hold the Discord gateway and both lease from the delivery queue.

**A migration that fails costs nothing.** `app` waits on
`migrate: condition: service_completed_successfully`, so `up -d` runs `alembic upgrade head` to
completion first and aborts if it fails. The old container is never stopped. The deploy simply
does not happen, Discord is told, and the bot goes on serving the previous build.

## What it costs to restart

Less than the compose file's comment implies, in the ordinary case.

`stop_grace_period` is 30s and the worker is *asked* to stop rather than cancelled, so it finishes
the delivery in hand and hands the rest of its batch straight back to the queue
(`worker.run_once` releases `deliveries[index:]` the moment `_stopping` is set). The fifteen
minute lease stall in the README is what a **SIGKILL** costs, not a clean stop. A clean stop
costs at most the one delivery that was mid-Discord-call when the stop arrived and did not finish
inside `worker_shutdown_grace_seconds`, which is 5s.

What it does cost, every time, and what nothing here can avoid:

- **Roughly 20 to 40 seconds with no endpoint.** Caddy stays up and returns 502, because the app
  is what it proxies to. **GitHub does not retry a failed webhook delivery.** Anything that
  arrives in that window is lost and has to be redelivered by hand from the repository's webhook
  page. This is the largest real cost of any deploy on this stack, automated or not, and it is
  the reason the `stable` gate is the recommended setting: a deploy you chose is a deploy you can
  choose the time of.
- **A gateway reconnect.** discord.py resumes or re-identifies; a few seconds, occasionally more
  if it is sitting out a rate limit.
- **A no-op `alembic upgrade head`,** two or three seconds, on every deploy.

## Setting it up

On the server, once:

    scp -r deploy root@5.181.50.209:/tmp/shannon-deploy
    ssh root@5.181.50.209 'bash /tmp/shannon-deploy/install.sh'

`install.sh` installs `skopeo`, `jq` and `curl`, puts the script at `/usr/local/sbin/shannon-deploy`,
puts the units in `/etc/systemd/system`, and stops. It prints the four things left: the Discord
webhook, `SHANNON_IMAGE_TAG` in `/opt/shannon/.env`, a `shannon-deploy --check` that proves it can
read the registry, and `systemctl enable --now shannon-deploy.timer`.

## Finding out that it happened

    journalctl -u shannon-deploy -n 200        # the last runs, in full
    cat /var/lib/shannon-deploy/deployed       # one line per deploy, ever
    cat /var/lib/shannon-deploy/last-check     # written on every run, deploy or not
    shannon-deploy --check                     # what it would do right now
    curl -s https://shannon.matthewdamholdt.dev/health | jq .version

A run that finds nothing logs nothing to the journal. Two hundred and eighty-eight lines a day of
`unchanged` is how a log stops being read; `last-check` carries the "it is still running" signal
instead, with a timestamp.

The Discord webhook is the part worth setting up. A deploy that lands, a deploy that fails, a
rollback and a registry it cannot read all post to a channel. Without it nobody learns that a
deploy failed at 03:00 until they wonder why the bot is quiet.

## Stopping it

    touch /opt/shannon/deploy.hold      # checks keep running, deploys do not
    rm /opt/shannon/deploy.hold         # on again
    systemctl disable --now shannon-deploy.timer

A failed deploy writes `deploy.hold` itself, with the reason inside, so a broken image is
deployed once and not every five minutes for the rest of the night.

## Rolling back

Automatic, when the new container never reports healthy or `/health` answers a commit other than
the one just deployed. The script pins `SHANNON_IMAGE_TAG=sha-<previous commit>` in
`/opt/shannon/.env` (the old file is kept as `.env.before-rollback`), brings the old image back,
and holds further deploys.

**It does not roll back the database.** `alembic upgrade head` has already run and committed. The
image rollback is only safe for a migration the previous code can still live with, which is most
of them and not all of them. The Discord message says this in as many words every time it
happens, because the situation it leaves is old code against a new schema and that is not
something to discover by reading a log.

By hand:

    cd /opt/shannon
    touch deploy.hold
    sed -i 's/^SHANNON_IMAGE_TAG=.*/SHANNON_IMAGE_TAG=sha-<commit>/' .env
    docker compose -f compose.prod.yaml pull app migrate
    docker compose -f compose.prod.yaml up -d

To go forward again, put `SHANNON_IMAGE_TAG` back to `stable` and `rm deploy.hold`.

## Uninstalling

    systemctl disable --now shannon-deploy.timer
    rm /etc/systemd/system/shannon-deploy.{service,timer} /usr/local/sbin/shannon-deploy
    rm -rf /etc/shannon-deploy.env /var/lib/shannon-deploy
    systemctl daemon-reload

Nothing in the stack depends on any of it. `pull` then `up -d` by hand works exactly as it did.
