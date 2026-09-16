#!/usr/bin/env bash
#
# One-time setup, run as root on the server, from a copy of this directory.
#
#     scp -r deploy root@5.181.50.209:/tmp/shannon-deploy
#     ssh root@5.181.50.209 'bash /tmp/shannon-deploy/install.sh'
#
# Installs the script, the timer, and the two packages the script needs. It does not start the
# timer: it prints the remaining manual steps and leaves you to check them first.

set -Eeuo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run this as root" >&2; exit 1; }

# skopeo asks the registry for a manifest without downloading layers, which is the whole reason
# the check is cheap enough to run every five minutes. jq reads its answer and builds the Discord
# payload. Both are in Debian 13.
apt-get update
apt-get install -y --no-install-recommends skopeo jq curl

install -m 0755 "$here/shannon-deploy.sh" /usr/local/sbin/shannon-deploy
install -m 0644 "$here/shannon-deploy.service" /etc/systemd/system/shannon-deploy.service
install -m 0644 "$here/shannon-deploy.timer" /etc/systemd/system/shannon-deploy.timer

if [ ! -e /etc/shannon-deploy.env ]; then
  install -m 0600 "$here/shannon-deploy.env.example" /etc/shannon-deploy.env
  echo "wrote /etc/shannon-deploy.env from the example"
else
  echo "/etc/shannon-deploy.env already exists, left alone"
fi

systemctl daemon-reload

cat <<'NEXT'

Installed. Four things left, in this order:

  1. Put a Discord webhook URL in /etc/shannon-deploy.env, or accept that the only record of a
     deploy is `journalctl -u shannon-deploy`.

  2. Decide what the box follows, in /opt/shannon/.env:

        SHANNON_IMAGE_TAG=stable   # deploys when you push a v* tag. Recommended.
        SHANNON_IMAGE_TAG=edge     # deploys on every green push to main.

     There is no default written for you, because the compose file falls back to `edge` and
     choosing that by omission is exactly the decision worth making on purpose.

  3. Prove it can read the registry and agrees with what is running:

        shannon-deploy --check

     It should print the tag, a registry digest, a running digest, and `nothing to do`. If it
     cannot read the registry, `docker login ghcr.io` again: the token in /root/.docker/config.json
     has probably expired.

  4. Start it:

        systemctl enable --now shannon-deploy.timer
        systemctl list-timers shannon-deploy.timer

To stop automatic deploys at any time without unpicking any of this:

        touch /opt/shannon/deploy.hold

NEXT
