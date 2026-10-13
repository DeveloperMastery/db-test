
set -euo pipefail
systemctl stop postgresql@18-main || true
systemctl stop postgresql || true
rm -rf /var/lib/postgresql/18/main
install -d -o postgres -g postgres -m 700 /var/lib/postgresql/18/main
sudo -u postgres env PGPASSFILE=/var/lib/postgresql/.pgpass \
  pg_basebackup -h 10.130.0.4 -p 5432 -U replicator \
  -D /var/lib/postgresql/18/main -Fp -Xs -P -R -C -S pg_limit_replica
echo "STEP|basebackup-done"
