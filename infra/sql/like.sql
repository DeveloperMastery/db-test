\set post_coin random(1, 100)
\if :post_coin <= 80
\set pid random(80000001, 100000000)
\else
\set pid random(1, 100000000)
\endif
\set user_coin random(1, 100)
\if :user_coin <= 80
\set uid random(8000001, 10000000)
\else
\set uid random(1, 10000000)
\endif
WITH inserted AS (
    INSERT INTO likes (post_id, user_id, created_at)
    VALUES (:pid, :uid, now())
    ON CONFLICT DO NOTHING
    RETURNING post_id
)
UPDATE posts AS p
SET like_count = p.like_count + 1
FROM inserted AS i
WHERE p.id = i.post_id;
