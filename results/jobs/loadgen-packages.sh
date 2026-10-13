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
apt-get install -y postgresql-common ca-certificates curl gnupg sysstat tmux python3
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt-get install -y postgresql-client-18 postgresql-18
systemctl disable --now postgresql@18-main || true
systemctl disable --now postgresql || true
python3 - <<'PY'
from pathlib import Path
pw = Path("/root/pg-limit/db.pass").read_text().strip()
Path("/root/.pgpass").write_text("10.130.0.4:5432:social:bench:" + pw + "\n")
Path("/root/.pgpass").chmod(0o600)
PY
install -d -m 755 /opt/pg-limit/sql /var/tmp/pg-limit
pgbench --version
echo "STEP|loadgen-done"
