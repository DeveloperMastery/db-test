
set -e
mkdir -p /var/tmp/pg-limit/100m-v2-write-confirm-r1603-1
stdbuf -oL -eL pgbench -n -h 10.130.0.4 -p 5432 -U bench -d social \
  -M prepared -c 50 -j 8 \
  -f /opt/pg-limit/sql/like.sql@80 \
  -f /opt/pg-limit/sql/new_post.sql@20 \
  -T 330 -P 10 --rate=1603 \
  --log --log-prefix=/var/tmp/pg-limit/100m-v2-write-confirm-r1603-1/txlog \
  > /var/tmp/pg-limit/100m-v2-write-confirm-r1603-1/summary.txt 2>&1
