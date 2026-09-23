#!/usr/bin/env bash
# The deploy, as one command that either finishes or puts back what was there.
#
# Run it on the server, or from a laptop as:
#
#     ssh root@<host> /opt/shannon/scripts/deploy.sh             # deploy the head of main
#     ssh root@<host> /opt/shannon/scripts/deploy.sh <commit>    # deploy a named commit
#     ssh root@<host> /opt/shannon/scripts/deploy.sh --rollback  # back to the previous image
#
# Nothing here is new work. It is the runbook in the README, in the order the README gives it,
# with the three checks a person does by eye done by the machine instead: that an image for this
# commit exists at all, that nothing is mid-delivery when the container is replaced, and that what
# came up is the commit that was asked for and is answering healthy.
#
# It is deliberately NOT triggered by a push. A green push restarting a live bot with nobody
# watching costs up to nine stalled deliveries and up to fifteen minutes of mirroring that
# silently does not happen, and the person who merged is the only one who knows whether now is a
# good moment to spend that. What is automated here is the procedure, not the decision.
#
# Two things it changes about the deployment, both on purpose:
#
#   - The image is pinned. SHANNON_IMAGE_TAG is written into .env as sha-<commit> rather than left
#     on the moving edge tag. That is what makes a rollback a thing you can name, what makes a
#     `docker compose up -d` typed by hand afterwards bring back the same build rather than
#     whatever edge moved to overnight, and what guarantees compose re-runs migrate: a changed tag
#     string is a changed container config, and an unchanged one is not.
#   - /opt/shannon is a git clone rather than a handful of files copied there once, so
#     compose.prod.yaml and the Caddyfile arrive from the same commit as the image. They are
#     versioned with the code and the old process never updated them. Arriving in the clone is
#     not the same as reaching the process that reads them, which is why caddy is recreated
#     below rather than left to compose.

set -euo pipefail

STACK_DIR="${SHANNON_STACK_DIR:-/opt/shannon}"
COMPOSE_FILE="compose.prod.yaml"
ENV_FILE=".env"
IMAGE="ghcr.io/canon-regularis/shannon-bot"
REMOTE="${SHANNON_GIT_REMOTE:-origin}"

# How long to wait for deliveries in flight to finish before replacing the container. Anything
# still leased when the worker dies sits untouched until worker_lease_seconds lapses, so this is
# the cheapest minute this script spends.
QUIET_WAIT_SECONDS="${SHANNON_QUIET_WAIT_SECONDS:-120}"
# How long to wait for the new container to report the commit we asked for, healthy. A cold start
# migrates, opens a gateway connection to Discord and sits out the image healthcheck start period.
VERIFY_TIMEOUT_SECONDS="${SHANNON_VERIFY_TIMEOUT_SECONDS:-180}"
# A rollback is a known-good image against a warm database, so it gets less rope.
ROLLBACK_TIMEOUT_SECONDS="${SHANNON_ROLLBACK_TIMEOUT_SECONDS:-120}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[1;31m!!! %s\033[0m\n" "$*" >&2; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ---------------------------------------------------------------------------------------------
# Arguments

TARGET_REF="${REMOTE}/main"
DO_ROLLBACK=0
FORCE=0
SKIP_QUIET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) DO_ROLLBACK=1 ;;
    --force)    FORCE=1 ;;
    --no-wait)  SKIP_QUIET=1 ;;
    -h|--help)  sed -n "2,28p" "$0" | sed "s/^#\\{0,1\\} \\{0,1\\}//"; exit 0 ;;
    -*)         die "unknown option: $1" ;;
    *)          TARGET_REF="$1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------------------------
# Preflight. Everything that can be known before anything is touched.

[ "$(id -u)" -eq 0 ] || die "run as root: the stack, its volumes and .env all belong to root"

cd "$STACK_DIR" 2>/dev/null || die "$STACK_DIR does not exist"
[ -f "$COMPOSE_FILE" ] || die "no $COMPOSE_FILE in $STACK_DIR"
[ -f "$ENV_FILE" ]     || die "no $ENV_FILE in $STACK_DIR; it holds the secrets and is never in git"
[ -d .git ]            || die "$STACK_DIR is not a git clone. See 'Deploying' in the README."

for tool in git curl jq docker flock; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed (apt-get install -y $tool)"
done

# Two deploys at once would race each other writing .env and replacing the same container. One ssh
# session that seemed to hang and a second opened beside it is all that takes.
exec 9>"$STACK_DIR/.deploy.lock"
flock -n 9 || die "another deploy is already running in $STACK_DIR"

# The compose project name comes from this directory's name, and the database lives in a volume
# named after it. Renaming the directory silently starts an empty Postgres.
[ "$(basename "$PWD")" = "shannon" ] ||
  die "the stack directory must be named 'shannon', or the volume names change and the database looks empty"

HOSTNAME_VALUE="$(sed -n "s/^SHANNON_HOSTNAME=//p" "$ENV_FILE" | tail -n 1)"
[ -n "$HOSTNAME_VALUE" ] || die "SHANNON_HOSTNAME is not set in $ENV_FILE"
HEALTH_URL="${SHANNON_HEALTH_URL:-https://${HOSTNAME_VALUE}/health}"

CURRENT_TAG="$(sed -n "s/^SHANNON_IMAGE_TAG=//p" "$ENV_FILE" | tail -n 1)"
[ -n "$CURRENT_TAG" ] || CURRENT_TAG="edge"

# ---------------------------------------------------------------------------------------------
# What /health says right now.
#
# Read with -w rather than -f because an unhealthy app answers 503, and the body of that 503 still
# carries the commit. "The new build is up and broken" and "the new build never came up" are
# different problems with different fixes, and this is the only thing that tells them apart.

read_health() {
  local body code
  body="$(mktemp)"
  code="$(curl -sS -o "$body" -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo 000)"
  HEALTH_CODE="$code"
  if [ "$code" = "200" ] || [ "$code" = "503" ]; then
    HEALTH_VERSION="$(jq -r ".version // \"unknown\"" <"$body" 2>/dev/null || echo unknown)"
    HEALTH_OK="$(jq -r ".healthy // false" <"$body" 2>/dev/null || echo false)"
    HEALTH_BODY="$(cat "$body")"
  else
    HEALTH_VERSION="unreachable"
    HEALTH_OK="false"
    HEALTH_BODY=""
  fi
  rm -f "$body"
}

say "Reading $HEALTH_URL"
read_health
RUNNING_BEFORE="$HEALTH_VERSION"
info "http $HEALTH_CODE  healthy=$HEALTH_OK  version=$RUNNING_BEFORE  tag in .env=$CURRENT_TAG"

# Where to go back to, worked out from what is ACTUALLY running rather than from what .env says.
# The two differ in the case that matters most: on a box still on the moving edge tag, .env says
# `edge` and edge has already moved to the build about to be deployed, so restoring the .env value
# would roll back to the thing being rolled back from. What /health reports is a commit, and
# sha-<commit> names exactly one build for ever.
if printf "%s" "$RUNNING_BEFORE" | grep -Eq "^[0-9a-f]{40}$"; then
  ROLLBACK_TAG="sha-${RUNNING_BEFORE}"
else
  # Nothing is running, or it is a hand-built image reporting `unknown`. There is no known-good
  # build to name, so the .env value is the only answer available and it may be a moving tag.
  ROLLBACK_TAG="$CURRENT_TAG"
  info "no commit from /health; rollback would restore the tag '$CURRENT_TAG', which may move"
fi

# ---------------------------------------------------------------------------------------------
# Work out the commit to deploy.

say "Fetching $REMOTE"
git fetch --quiet --prune "$REMOTE" || die "git fetch failed; the box cannot reach GitHub"

if [ "$DO_ROLLBACK" -eq 1 ]; then
  PREVIOUS_TAG="$(cat .deploy-previous-tag 2>/dev/null || true)"
  [ -n "$PREVIOUS_TAG" ] ||
    die "no .deploy-previous-tag from an earlier deploy; name the commit to go back to instead"
  case "$PREVIOUS_TAG" in
    sha-*) TARGET_COMMIT="${PREVIOUS_TAG#sha-}" ;;
    *) die "the previous tag is '$PREVIOUS_TAG', which moves and does not name a build.
    Pick the commit you want from 'git log' and pass it instead." ;;
  esac
  info "rolling back to $PREVIOUS_TAG"
else
  TARGET_COMMIT="$(git rev-parse --verify "${TARGET_REF}^{commit}" 2>/dev/null || true)"
  [ -n "$TARGET_COMMIT" ] || die "cannot resolve '$TARGET_REF' to a commit"
fi
TARGET_TAG="sha-${TARGET_COMMIT}"

info "target  $TARGET_COMMIT"
info "running $RUNNING_BEFORE"

if [ "$RUNNING_BEFORE" = "$TARGET_COMMIT" ] && [ "$HEALTH_OK" = "true" ] && [ "$FORCE" -eq 0 ]; then
  say "Already running $TARGET_COMMIT and healthy. Nothing to do."
  exit 0
fi

# The check that turns the most common failure into a sentence instead of a mystery. No image for
# this commit means CI has not finished, or CI was red and never published. Neither is a reason to
# touch a running bot.
say "Checking the registry for $TARGET_TAG"
if ! docker manifest inspect "${IMAGE}:${TARGET_TAG}" >/dev/null 2>&1; then
  die "no image ${IMAGE}:${TARGET_TAG}.
    Either CI has not published this commit yet, CI failed and never published it, or the GHCR
    token in root's docker config has expired: 'docker login ghcr.io' rules the last one out.
    Nothing has been changed."
fi

# The stack files move to the target commit before anything runs, so compose.prod.yaml, the
# Caddyfile and this script are all that commit's. This script is running from a file git is about
# to rewrite, so it hands over to a copy rather than reading on past the edit.
if [ "$(git rev-parse HEAD)" != "$TARGET_COMMIT" ]; then
  say "Checking out $TARGET_COMMIT"
  git checkout --quiet --detach "$TARGET_COMMIT"
fi
if [ -z "${SHANNON_DEPLOY_REEXEC:-}" ]; then
  self="$(mktemp)"
  cat scripts/deploy.sh >"$self"
  chmod +x "$self"
  args=("$TARGET_COMMIT")
  [ "$FORCE" -eq 1 ] && args+=("--force")
  [ "$SKIP_QUIET" -eq 1 ] && args+=("--no-wait")
  export SHANNON_DEPLOY_REEXEC=1
  exec "$self" "${args[@]}"
fi

# ---------------------------------------------------------------------------------------------
# Pull before stopping anything. A pull that fails has cost nothing.

say "Pulling ${IMAGE}:${TARGET_TAG}"
SHANNON_IMAGE_TAG="$TARGET_TAG" compose pull app migrate ||
  die "pull failed. Nothing has been changed."

# ---------------------------------------------------------------------------------------------
# Wait for the queue to go quiet.
#
# A worker killed mid-batch leaves every delivery it has leased and not yet finished sitting in
# PROCESSING until worker_lease_seconds (900) lapses: up to nine rows and up to fifteen minutes of
# mirroring that silently does not happen. Waiting a minute for the batch in hand to drain is how
# that bill is avoided, and there is no other way to avoid it.

# The two settings psql needs, read the same way every other value here is read. Sourcing .env
# would have been shorter and is wrong: it holds `SHANNON_ROLE_PROJECT_MANAGER=Project Manager`
# and a dozen lines like it, so `. ./.env` under `set -e` runs `Manager` as a command and kills
# the deploy before it starts. Nothing in this file needs .env as an environment; compose reads
# it directly.
PG_USER="$(sed -n "s/^POSTGRES_USER=//p" "$ENV_FILE" | tail -n 1)"
PG_DB="$(sed -n "s/^POSTGRES_DB=//p" "$ENV_FILE" | tail -n 1)"
[ -n "$PG_USER" ] || PG_USER="shannon"
[ -n "$PG_DB" ] || PG_DB="shannon"

leased_now() {
  compose exec -T db psql -qtAX -U "$PG_USER" -d "$PG_DB" \
    -c "SELECT count(*) FROM webhook_events WHERE status = 'PROCESSING' AND locked_until > now();" \
    2>/dev/null | tr -d "[:space:]"
}

if [ "$SKIP_QUIET" -eq 0 ]; then
  say "Waiting for deliveries in flight"
  deadline=$(($(date +%s) + QUIET_WAIT_SECONDS))
  while :; do
    n="$(leased_now || echo "?")"
    if [ "$n" = "0" ]; then
      info "queue is quiet"
      break
    fi
    if [ -z "$n" ] || [ "$n" = "?" ]; then
      info "cannot read the queue; carrying on"
      break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      if [ "$FORCE" -eq 1 ]; then
        info "$n still leased; --force given, carrying on"
        break
      fi
      die "$n deliveries are still leased after ${QUIET_WAIT_SECONDS}s.
    Replacing the container now strands them for up to fifteen minutes. Wait and run this again,
    or pass --force to accept that. Nothing has been changed."
    fi
    info "$n leased, waiting"
    sleep 5
  done
fi

# ---------------------------------------------------------------------------------------------
# Pin the tag, migrate, then swap.

write_tag() {
  local tag="$1" tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  { grep -v "^SHANNON_IMAGE_TAG=" "$ENV_FILE" || true; } >"$tmp"
  printf "SHANNON_IMAGE_TAG=%s\n" "$tag" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

printf "%s\n" "$ROLLBACK_TAG" >.deploy-previous-tag
write_tag "$TARGET_TAG"

# The migration runs as its own step rather than as a dependency of `up`, so a migration that
# fails is a migration that failed, said in one line, with the old container still serving.
say "Migrating"
if ! compose run --rm migrate; then
  write_tag "$CURRENT_TAG"
  die "alembic upgrade head failed. The old container is untouched and still running
    $RUNNING_BEFORE. The .env tag has been put back. Read the output above; nothing else changed."
fi

say "Starting $TARGET_COMMIT"
compose up -d || info "compose up reported an error; the verification below decides"

# Caddy is recreated by hand, every time, because nothing else will do it.
#
# The Caddyfile is versioned and arrives with the checkout above, and `compose up -d` leaves the
# container alone: the image is the same and the mount is the same, and a bind-mounted file's
# CONTENTS changing is not a change to a container's config. That is the same rule that makes the
# app restart — a moved tag string IS a config change — working the other way round.
#
# Worse than stale, and this is the part that cost days: Docker binds a single file by inode, and
# `git checkout` replaces the file rather than editing it. So after a pull the container is still
# reading the old, now-unlinked inode. `caddy reload` does not help and reports success anyway,
# because it re-reads a path inside the container that no longer tracks the host. Recreating is
# what re-resolves the mount.
#
# Unconditional rather than only when the file changed. Comparing would mean asking the container
# what it can see, which is the thing in question, and this costs a few seconds in the middle of
# a deploy that is already restarting the app. The certificates are in a named volume, so nothing
# is re-requested from Let's Encrypt.
compose up -d --force-recreate caddy ||
  info "could not recreate caddy; a proxy rule shipped in this commit may not be live"

# ---------------------------------------------------------------------------------------------
# Verify against the public URL, the same path GitHub and everyone else uses, so it proves the
# certificate and that Caddy is up, as well as the app.
#
# Not that Caddy is forwarding correctly, which is a different claim and is checked separately
# after this succeeds. `/health` was forwarded by every config this proxy has ever run, so it
# cannot tell a working routing table from one missing everything else.

verify() {
  local want="$1" timeout="$2" deadline
  deadline=$(($(date +%s) + timeout))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    read_health
    if [ "$HEALTH_VERSION" = "$want" ] && [ "$HEALTH_OK" = "true" ]; then
      return 0
    fi
    printf "    http %s  healthy=%-5s version=%s\n" "$HEALTH_CODE" "$HEALTH_OK" "$HEALTH_VERSION"
    sleep 5
  done
  return 1
}

# Whether the proxy forwards a route that is not `/health`.
#
# `verify` above proves the app is up and that ONE path reaches it. That is not the same claim: an
# allowlist that forwards `/health` and nothing else passes it forever, which is exactly what
# happened to `/oauth/*`. Every layer reported success — the pull, the compose up, even a manual
# `caddy reload` — over a config that was never live, and this was the check that could have said
# so and did not.
#
# A 404 is the tell, because it is what the Caddyfile's catch-all answers. Anything else means the
# request reached the app, including the 400 this path gives when it is opened with no parameters
# and the 500 a deployment with no OAuth configured gives. Which of those it is, is the app's
# business rather than this script's.
proxy_reaches() {
  local code
  code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${HOSTNAME_VALUE}$1" \
    2>/dev/null || echo 000)"
  [ "$code" != "404" ] && [ "$code" != "000" ]
}

say "Verifying"
if verify "$TARGET_COMMIT" "$VERIFY_TIMEOUT_SECONDS"; then
  if proxy_reaches /oauth/github/callback; then
    say "Deployed $TARGET_COMMIT"
  else
    say "Deployed $TARGET_COMMIT, but the proxy is not forwarding everything"
    info "https://${HOSTNAME_VALUE}/oauth/github/callback answered 404, which is Caddy's"
    info "catch-all rather than this bot. /link and /unregister hand out links to that path,"
    info "so both are broken until it is fixed. Check the Caddyfile has a handle block for it"
    info "and recreate the container:"
    info "    docker compose -f $COMPOSE_FILE up -d --force-recreate caddy"
  fi
  info "$HEALTH_BODY"
  exit 0
fi

# ---------------------------------------------------------------------------------------------
# It did not come up. Put the previous image back.

say "FAILED to verify $TARGET_COMMIT"
LOG="/var/log/shannon-failed-${TARGET_COMMIT}.log"
compose logs --no-color --tail=200 app >"$LOG" 2>&1 || true
info "app logs saved to $LOG; last lines:"
compose logs --no-color --tail=40 app 2>&1 | sed "s/^/    /" || true

# Rolling back needs somewhere to roll back TO. A first install, or a deploy attempted while the
# bot was already down, has no known-good commit: /health reported no version, so the only tag
# available is whatever .env happened to say, and putting that back would be a guess dressed up
# as a recovery. Say so instead and stop, with the logs already saved.
if ! printf "%s" "$RUNNING_BEFORE" | grep -Eq "^[0-9a-f]{40}$"; then
  cat <<EOF

    NOT ROLLING BACK: nothing was running before this, so there is no build to go back to.
    /health reported '$RUNNING_BEFORE' when this started.

    .env is pinned to $TARGET_TAG and the stack has been left as it is, so the logs above are
    the live ones. Read $LOG, fix it, and run this again.
EOF
  exit 2
fi

say "Rolling back to $ROLLBACK_TAG"
write_tag "$ROLLBACK_TAG"
SHANNON_IMAGE_TAG="$ROLLBACK_TAG" compose up -d || true

if verify "$RUNNING_BEFORE" "$ROLLBACK_TIMEOUT_SECONDS"; then
  cat <<EOF

    Rolled back to $ROLLBACK_TAG and it is answering healthy.

    THE DATABASE WAS NOT ROLLED BACK. The migration for $TARGET_COMMIT has been applied and
    alembic downgrade was NOT run, because a downgrade can drop a column and the data in it. The
    old code is now running against a newer schema. That is fine for an additive migration, which
    is nearly all of them, and is not fine for one that removed or renamed something the old code
    reads. If the bot misbehaves from here, that is the first place to look.
EOF
  exit 1
fi

cat <<EOF

    ROLLBACK ALSO FAILED. The bot is down.

    On the box, in $STACK_DIR:
      docker compose -f $COMPOSE_FILE ps
      docker compose -f $COMPOSE_FILE logs --tail=200 app
      docker compose -f $COMPOSE_FILE logs --tail=50 db

    .env is pinned to $ROLLBACK_TAG. Fix what the logs say, then:
      docker compose -f $COMPOSE_FILE up -d
EOF
exit 2
