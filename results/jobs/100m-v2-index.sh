sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\timing on
SET maintenance_work_mem = '1GB';
\echo STEP|users_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_pkey') THEN
        ALTER TABLE users ADD PRIMARY KEY (id);
    END IF;
END $$;
\echo STEP|posts_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'posts_pkey') THEN
        ALTER TABLE posts ADD PRIMARY KEY (id);
    END IF;
END $$;
\echo STEP|likes_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'likes_pkey') THEN
        ALTER TABLE likes ADD PRIMARY KEY (post_id, user_id);
    END IF;
END $$;
\echo STEP|posts_user_created_idx
CREATE INDEX IF NOT EXISTS posts_user_created_idx ON posts (user_id, created_at DESC);
\echo STEP|setval
SELECT setval(pg_get_serial_sequence('posts', 'id'), (SELECT max(id) FROM posts));
\echo STEP|identity
SELECT 'identity|' || pg_sequence_last_value(pg_get_serial_sequence('posts','id')::regclass)::text;
\echo STEP|vacuum
VACUUM ANALYZE users, posts, likes;
\echo STEP|checkpoint
CHECKPOINT;
SQL
