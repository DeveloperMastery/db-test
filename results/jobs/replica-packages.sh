#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
echo "STEP|wait-apt"
for i in $(seq 1 90); do
  if pgrep -x apt-get >/dev/null || pgrep -x apt >/dev/null || pgrep -x dpkg >/dev/null; then
    echo "STEP|apt-busy"
    sleep 10
  else
    break
  fi
done
ok=0
for i in $(seq 1 10); do
  if apt-get update -y; then
    ok=1
    break
  fi
  echo "STEP|apt-update-retry"
  sleep 15
done
test "$ok" = 1
apt-get install -y postgresql-common ca-certificates curl gnupg sysstat tmux
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt-get install -y postgresql-18 postgresql-contrib-18
systemctl stop postgresql
echo "STEP|packages"
