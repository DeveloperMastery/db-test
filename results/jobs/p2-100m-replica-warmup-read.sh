
set -e
mkdir -p /var/tmp/pg-limit/100m-replica-warmup/read
stdbuf -oL -eL pgbench -n -h 10.130.0.3 -p 5432 -U bench -d social \
  -M prepared -c 50 -j 8 \
  -f /opt/pg-limit/sql/profile.sql@50 -f /opt/pg-limit/sql/open_post.sql@50 \
  -T 2400 -P 60  \
  --log --log-prefix=/var/tmp/pg-limit/100m-replica-warmup/read/txlog \
  > /var/tmp/pg-limit/100m-replica-warmup/read/summary.txt 2>&1
