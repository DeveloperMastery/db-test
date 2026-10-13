sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\timing on
\echo STEP|users
SELECT 'users|' || count(*)::text FROM users;
\echo STEP|posts
SELECT 'posts|' || count(*)::text FROM posts;
\echo STEP|likes
SELECT 'likes|' || count(*)::text FROM likes;
\echo STEP|like_count
SELECT 'like_count|' || min(like_count)::text || ',' || max(like_count)::text || ',' ||
       count(*) FILTER (WHERE like_count <> 2)::text FROM posts;
\echo STEP|bodies
SELECT 'bodies|' || min(length(body))::text || ',' || max(length(body))::text FROM posts;
SQL
