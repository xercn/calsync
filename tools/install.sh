#!/usr/bin/env bash
# Run on the Mac: install the currently committed version to the server. Dry run by default; --apply to write.
#
#   tools/install.sh                 dry run: show the remote state and what would be done (read-only)
#   tools/install.sh --apply         install / update code, user, directories, systemd units (timer not enabled)
#   tools/install.sh --apply --enable  same, and also enable --now calsync-poll.timer
#
# Scheduling: calsync-poll.timer fires every minute and is the only timer -- it checks the config page (interval,
# sync-now, confirmed settings) and runs a full sync only when due (default 10 minutes). The old calsync.timer is
# retired: it is stopped and removed on install; calsync.service stays for manual runs.
#   tools/install.sh --revert [--apply]  disable timer, remove units and /opt/calsync (keeps /etc/calsync, /var/lib/calsync)
#
# Remote host: set CALSYNC_HOST (user@host), CALSYNC_PORT (default 22), CALSYNC_KEY (ssh key).
set -euo pipefail

HOST=${CALSYNC_HOST:?set CALSYNC_HOST=user@host}
PORT=${CALSYNC_PORT:-22}
KEY=${CALSYNC_KEY:-$HOME/.ssh/id_ed25519}
SSH=(ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$KEY" -p "$PORT" "$HOST")

APPLY=0 ENABLE=0 REVERT=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --enable) ENABLE=1 ;;
    --revert) REVERT=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

if [ "$REVERT" = 1 ]; then
  echo "== revert on $HOST (keeps /etc/calsync and /var/lib/calsync)"
  if [ "$APPLY" = 0 ]; then
    "${SSH[@]}" 'systemctl is-enabled calsync.timer 2>&1; ls -d /opt/calsync /opt/calsync.prev /etc/systemd/system/calsync.* 2>&1'
    echo "(dry-run) would: systemctl disable --now calsync.timer calsync-poll.timer; rm unit files; daemon-reload; rm -rf /opt/calsync /opt/calsync.prev"
    exit 0
  fi
  "${SSH[@]}" 'set -e
    systemctl disable --now calsync.timer calsync-poll.timer 2>/dev/null || true
    systemctl stop calsync.service calsync-poll.service 2>/dev/null || true
    rm -f /etc/systemd/system/calsync.service /etc/systemd/system/calsync.timer /etc/systemd/system/calsync-poll.service /etc/systemd/system/calsync-poll.timer
    systemctl daemon-reload
    rm -rf /opt/calsync /opt/calsync.prev
    echo reverted'
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree not clean; commit first (production runs committed code only)" >&2
  exit 1
fi
REV=$(git rev-parse --short HEAD)

echo "== remote state ($HOST, read-only)"
"${SSH[@]}" 'id calsync 2>&1; cat /opt/calsync/VERSION 2>/dev/null || echo "not installed";
  ls -l /etc/calsync 2>&1 | sed "s/^/  /"; systemctl is-enabled calsync.timer calsync-poll.timer 2>&1;
  /usr/bin/python3 -c "import requests, cryptography; print(\"deps ok\")"'

echo
echo "== plan: install $REV"
cat <<EOF
  - user calsync (system, nologin, home /var/lib/calsync) if missing
  - /opt/calsync <- git archive $REV (old one kept as /opt/calsync.prev)
  - /etc/calsync (root:calsync 750); config.toml from config.example.toml only if missing
  - /var/lib/calsync (calsync 700)
  - /etc/systemd/system/calsync.service, calsync-poll.{service,timer}; daemon-reload
  - old calsync.timer (fixed 10 min): disable --now and remove (calsync-poll.timer schedules runs now)
  - $( [ "$ENABLE" = 1 ] && echo "enable --now calsync-poll.timer" || echo "calsync-poll.timer NOT enabled by this run (add --enable); if already enabled it keeps running")
EOF
[ "$APPLY" = 1 ] || { echo; echo "(dry-run) add --apply to install"; exit 0; }

git archive --format=tar "$REV" | "${SSH[@]}" "set -e
  id calsync >/dev/null 2>&1 || useradd --system --home-dir /var/lib/calsync --shell /usr/sbin/nologin calsync
  rm -rf /opt/calsync.new && mkdir /opt/calsync.new
  tar -x -C /opt/calsync.new
  echo $REV > /opt/calsync.new/VERSION
  chown -R root:root /opt/calsync.new && chmod -R go-w /opt/calsync.new
  if [ -d /opt/calsync ]; then rm -rf /opt/calsync.prev; mv /opt/calsync /opt/calsync.prev; fi
  mv /opt/calsync.new /opt/calsync
  install -d -o root -g calsync -m 750 /etc/calsync
  [ -f /etc/calsync/config.toml ] || install -o root -g calsync -m 640 /opt/calsync/config.example.toml /etc/calsync/config.toml
  install -d -o calsync -g calsync -m 700 /var/lib/calsync
  install -m 644 /opt/calsync/deploy/calsync.service /opt/calsync/deploy/calsync-poll.service /opt/calsync/deploy/calsync-poll.timer /etc/systemd/system/
  systemctl disable --now calsync.timer 2>/dev/null || true
  rm -f /etc/systemd/system/calsync.timer
  systemctl daemon-reload
  if [ $ENABLE = 1 ]; then systemctl enable --now calsync-poll.timer; fi
  echo installed \$(cat /opt/calsync/VERSION); systemctl is-enabled calsync-poll.timer || true"
