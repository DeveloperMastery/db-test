\set user_coin random(1, 100)
\if :user_coin <= 80
\set uid random(8000001, 10000000)
\else
\set uid random(1, 10000000)
\endif
\set len random(80, 200)
INSERT INTO posts (user_id, body, created_at)
VALUES (:uid, repeat('x', :len), now());
