#!/usr/bin/env bash
#
# Deploy the Shannon bot when, and only when, the registry holds a different image from the one
# the app container is running.
#
# The server pulls. Nothing is pushed to it, no workflow holds a key to this machine, and the
# only thing that leaves the box is a manifest request to ghcr.io every few minutes.
#
# Run by shannon-deploy.timer. Run it by hand the same way, as root:
#
#     shannon-deploy            # check, and deploy if there is something new
#     shannon-deploy --force    # deploy whatever the tag points at, even if it looks current
#     shannon-deploy --check    # say what it would do and change nothing
#
# Three things about the shape of this. The digest comparison is only an optimisation: `docker
# compose up -d` does not recreate a container whose image and configuration are unchanged, so a
# spurious run costs a no-op `alembic upgrade head` and nothing else. The migration runs before
# the app is touched, so a migration that fails leaves the old container running and the deploy
# simply does not happen. And the rollback below puts the image back and cannot put the database
# back, which is the one thing here worth reading twice.

set -Eeuo pipefail

CONFIG_FILE=${SHANNON_DEPLOY_CONFIG:-/etc/shannon-deploy.env}
# shellcheck source=/dev/null
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"

DIR=${SHANNON_DEPLOY_DIR:-/opt/shannon}
COMPOSE_FILE=${SHANNON_DEPLOY_COMPOSE:-compose.prod.yaml}
IMAGE=${SHANNON_DEPLOY_IMAGE:-ghcr.io/canon-regularis/shannon-bot}
AUTHFILE=${SHANNON_DEPLOY_AUTHFILE:-/root/.docker/config.json}
STATE_DIR=${STATE_DIRECTORY:-/var/lib/shannon-deploy}
HOLD_FILE=${SHANNON_DEPLOY_HOLD_FILE:-$DIR/deploy.hold}
LOCK_FILE=${SHANNON_DEPLOY_LOCK_FILE:-/var/lock/shannon-deploy.lock}
# A 20s start period plus five 15s retries is how long the image's own HEALTHCHECK takes to give
# up, so anything shorter than that calls a slow start a failure.
HEALTH_TIMEOUT=${SHANNON_DEPLOY_HEALTH_TIMEOUT:-180}
DISCORD_WEBHOOK=${SHANNON_DEPLOY_DISCORD_WEBHOOK:-}
PRUNE_AFTER_HOURS=${SHANNON_DEPLOY_PRUNE_AFTER_HOURS:-720}

MODE=deploy
case "${1:-}" in
  --force) MODE=force ;;
  --check) MODE=check ;;
  --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }

# Said in Discord as well as in the journal, because the journal is somewhere nobody looks until
# they already suspect something. allowed_mentions is emptied so that nothing this posts can ping
# a room, whatever ends up interpolated into it.
tell() {
  say "$1"
  [ -n "$DISCORD_WEBHOOK" ] || return 0
  local payload
  payload=$(jq -nc --arg c "$1" '{content: $c, allowed_mentions: {parse: []}}')
  curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
    -d "$payload" "$DISCORD_WEBHOOK" >/dev/null \
    || say "could not post to the Discord webhook; the journal has the rest"
}

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# One key out of .env rather than sourcing the file, which holds the bot token, the GitHub token
# and the webhook secret and has no business in this process's environment.
env_value() {
  local key=$1 default=${2:-} line
  line=$(grep -m1 -E "^[[:space:]]*${key}=" "$DIR/.env" 2>/dev/null || true)
  [ -n "$line" ] || { printf '%s' "$default"; return 0; }
  printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/'
}

# What the app container is actually running, taken from the container rather than from a note
# this script keeps, so a deploy done by hand cannot leave the two disagreeing. Empty when it
# cannot be worked out, which is treated as "deploy and find out": an unchanged image makes
# `up -d` a no-op anyway.
running_digest() {
  local cid image_id
  cid=$(compose ps -q app 2>/dev/null) || return 0
  [ -n "$cid" ] || return 0
  image_id=$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null) || return 0
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_id" 2>/dev/null \
    | awk -F@ -v img="$IMAGE" '$1 == img { print $2; exit }'
}

# The registry's answer: one HTTPS request, no layers downloaded. --no-tags because listing every
# tag on this repository is the expensive half of an inspect and nothing here reads it.
registry_field() {
  skopeo inspect --authfile "$AUTHFILE" --no-tags "docker://$IMAGE:$TAG" 2>/dev/null | jq -r "$1"
}

# The commit answering behind Caddy. Proves TLS, Caddy and the app in one request, and is the only
# check here that looks at the service the way GitHub does.
served_version() {
  [ -n "$SITE" ] || return 0
  curl -fsS --max-time 10 "https://$SITE/health" 2>/dev/null | jq -r '.version // empty'
}

wait_until_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT )) cid status
  while [ "$(date +%s)" -lt "$deadline" ]; do
    cid=$(compose ps -q app 2>/dev/null || true)
    if [ -n "$cid" ]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo gone)
      case "$status" in
        healthy|none) return 0 ;;
        unhealthy) say "the app container reports unhealthy"; return 1 ;;
      esac
    fi
    sleep 3
  done
  say "the app container did not report healthy within ${HEALTH_TIMEOUT}s"
  return 1
}

# Rewriting one key in .env, through a temp file that compose has to accept before it is moved
# into place. A rollback that leaves the pin only in this process's environment is undone by the
# next `docker compose up -d` somebody types, which would silently put the broken image back.
pin_image_tag() {
  local value=$1 tmp
  tmp=$(mktemp "$DIR/.env.XXXXXX")
  chmod --reference="$DIR/.env" "$tmp"
  if grep -qE '^[[:space:]]*SHANNON_IMAGE_TAG=' "$DIR/.env"; then
    sed -E "s|^[[:space:]]*SHANNON_IMAGE_TAG=.*|SHANNON_IMAGE_TAG=${value}|" "$DIR/.env" >"$tmp"
  else
    { cat "$DIR/.env"; printf '\nSHANNON_IMAGE_TAG=%s\n' "$value"; } >"$tmp"
  fi
  cp -a "$DIR/.env" "$DIR/.env.before-rollback"
  mv "$tmp" "$DIR/.env"
  if ! compose config -q >/dev/null 2>&1; then
    mv "$DIR/.env.before-rollback" "$DIR/.env"
    say "the rewritten .env did not parse; the original is back"
    return 1
  fi
}

cd "$DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || { say "another run holds the lock; nothing to do"; exit 0; }

if [ -e "$HOLD_FILE" ] && [ "$MODE" != force ]; then
  # Quiet on purpose. This fires every five minutes and a held deploy is a deliberate state.
  exit 0
fi

mkdir -p "$STATE_DIR"

TAG=$(env_value SHANNON_IMAGE_TAG edge)
SITE=$(env_value SHANNON_HOSTNAME)

remote=$(registry_field '.Digest')
if [ -z "$remote" ] || [ "$remote" = null ]; then
  tell "shannon-deploy: cannot read $IMAGE:$TAG from the registry. An expired GHCR token is the usual reason; try \`skopeo inspect --authfile $AUTHFILE docker://$IMAGE:$TAG\`."
  exit 1
fi

local_=$(running_digest)
printf 'checked=%s tag=%s remote=%s running=%s\n' \
  "$(date -Is)" "$TAG" "$remote" "${local_:-unknown}" >"$STATE_DIR/last-check"

if [ "$MODE" = check ]; then
  say "tag      $TAG"
  say "registry $remote"
  say "running  ${local_:-unknown}"
  [ "$remote" = "$local_" ] && say "nothing to do" || say "would deploy"
  exit 0
fi

if [ "$remote" = "$local_" ] && [ "$MODE" != force ]; then
  # Nothing said, nowhere. 288 lines a day of "unchanged" is how a log stops being read.
  exit 0
fi

want=$(registry_field '.Labels["org.opencontainers.image.revision"] // empty')
had=$(served_version)
say "new image on $IMAGE:$TAG: $remote (revision ${want:-unknown}), replacing ${had:-unknown}"

trouble() {
  local what=$1
  compose logs --tail 60 app || true
  if [ -n "$had" ]; then
    say "rolling back to sha-$had"
    if pin_image_tag "sha-$had" && compose up -d && wait_until_healthy; then
      { date -Is; printf 'rolled back from %s to sha-%s\n' "$remote" "$had"; } >"$HOLD_FILE"
      tell "**shannon-deploy: the deploy failed and was rolled back.** ${what}. The image is back on \`${had:0:7}\`, \`.env\` is pinned to it, and automatic deploys are held until \`$HOLD_FILE\` is removed. **The database is still on the new revision** - if that migration was not backward compatible, the old code is now running against a schema it does not expect. \`journalctl -u shannon-deploy -n 200\`"
      exit 1
    fi
    tell "**shannon-deploy: the deploy failed AND the rollback failed.** ${what}. The bot is probably down. Get on the box: \`cd $DIR && docker compose -f $COMPOSE_FILE ps\`"
    exit 1
  fi
  tell "**shannon-deploy: the deploy failed** and there was no previous build to go back to. ${what}. \`journalctl -u shannon-deploy -n 200\`"
  exit 1
}

if ! compose pull app migrate; then
  tell "shannon-deploy: \`docker compose pull\` failed for $IMAGE:$TAG. Nothing was changed and the old build is still running."
  exit 1
fi

# `up -d` runs the migration to completion first and starts the app only if it exits zero, so a
# failure here means the old container is still up and untouched. That is the good case.
if ! compose up -d; then
  tell "shannon-deploy: \`up -d\` failed, which is almost always \`alembic upgrade head\` refusing. **The old build is still running and still serving** - nothing was replaced. \`cd $DIR && docker compose -f $COMPOSE_FILE logs migrate\`"
  exit 1
fi

wait_until_healthy || trouble "the new container never reported healthy"

now=$(served_version)
if [ -n "$want" ] && [ -n "$now" ] && [ "$now" != "$want" ]; then
  trouble "/health answers \`${now:0:7}\` but the image just deployed says \`${want:0:7}\`"
fi
if [ -n "$SITE" ] && [ -z "$now" ]; then
  trouble "https://$SITE/health did not answer after the deploy"
fi

printf 'deployed=%s tag=%s digest=%s revision=%s previous=%s\n' \
  "$(date -Is)" "$TAG" "$remote" "${want:-unknown}" "${had:-unknown}" >>"$STATE_DIR/deployed"

tell "shannon-deploy: **${TAG}** is now \`${want:0:7}\` (was \`${had:0:7}\`, blank if nothing was running). /health is green."

# Tagged sha- images are not dangling, so nothing else would ever remove them. Scoped by the label
# the release workflow writes, so this cannot reach postgres or caddy however old they are.
docker image prune -af \
  --filter "until=${PRUNE_AFTER_HOURS}h" \
  --filter "label=org.opencontainers.image.title=Shannon Bot" >/dev/null 2>&1 || true
