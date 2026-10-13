\set user_coin random(1, 100)
\if :user_coin <= 80
\set uid random(8000001, 10000000)
\else
\set uid random(1, 10000000)
\endif
SELECT p.id, p.body, p.created_at, p.like_count
FROM posts p
WHERE p.user_id = :uid
ORDER BY p.created_at DESC
LIMIT 20;
