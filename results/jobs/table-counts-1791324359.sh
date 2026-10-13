
sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT 'users|' || count(*) FROM users
UNION ALL SELECT 'posts|' || count(*) FROM posts
UNION ALL SELECT 'likes|' || count(*) FROM likes;
SQL
