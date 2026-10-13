\set post_coin random(1, 100)
\if :post_coin <= 80
\set pid random(80000001, 100000000)
\else
\set pid random(1, 100000000)
\endif
SELECT p.id, p.body, p.created_at, p.like_count
FROM posts p
WHERE p.id = :pid;
