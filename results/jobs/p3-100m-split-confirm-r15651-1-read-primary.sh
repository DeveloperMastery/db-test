
set -e
mkdir -p /var/tmp/pg-limit/100m-split-confirm-r15651-1/read-primary
stdbuf -oL -eL pgbench -n -h 10.130.0.4 -p 5432 -U bench -d social \
  -M prepared -c 50 -j 8 \
  -f /opt/pg-limit/sql/profile.sql@50 -f /opt/pg-limit/sql/open_post.sql@50 \
  -T 330 -P 10 --rate=7043 \
  --log --log-prefix=/var/tmp/pg-limit/100m-split-confirm-r15651-1/read-primary/txlog \
  > /var/tmp/pg-limit/100m-split-confirm-r15651-1/read-primary/summary.txt 2>&1
