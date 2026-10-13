\set user_coin random(1, 100)
\if :user_coin <= 80
\set uid random(8000001, 10000000)
\else
\set uid random(1, 10000000)
\endif
\set post_coin random(1, 100)
\if :post_coin <= 80
\set pid random(80000001, 100000000)
\else
\set pid random(1, 100000000)
\endif
INSERT INTO access_probe (user_id, post_id) VALUES (:uid, :pid);
