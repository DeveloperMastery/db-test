#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "STEP disable apt timers"
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
echo "STEP apt"
apt-get update -y
apt-get install -y postgresql-common ca-certificates curl gnupg sysstat python3
echo "STEP pgdg repo"
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
echo "STEP install client"
apt-get install -y postgresql-client-18
echo "STEP pgpass"
umask 077
printf '10.130.0.3:5432:social:bench:%s\n' "$(cat /root/pg-limit/db.pass)" > /root/.pgpass
chmod 600 /root/.pgpass
install -d -m 755 /opt/pg-limit/sql /var/tmp/pg-limit
pgbench --version
echo "STEP done"
