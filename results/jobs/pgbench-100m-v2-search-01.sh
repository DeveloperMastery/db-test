
set -e
mkdir -p /var/tmp/pg-limit/100m-v2-search-01
stdbuf -oL -eL pgbench -n -h 10.130.0.4 -p 5432 -U bench -d social \
  -M prepared -c 50 -j 8 \
  -f /opt/pg-limit/sql/profile.sql@45 \
  -f /opt/pg-limit/sql/open_post.sql@45 \
  -f /opt/pg-limit/sql/like.sql@8 \
  -f /opt/pg-limit/sql/new_post.sql@2 \
  -T 150 -P 10 --rate=5670 \
  --log --log-prefix=/var/tmp/pg-limit/100m-v2-search-01/txlog \
  > /var/tmp/pg-limit/100m-v2-search-01/summary.txt 2>&1
