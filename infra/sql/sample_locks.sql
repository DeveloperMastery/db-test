SELECT 'summary',
       extract(epoch FROM now())::bigint,
       (SELECT count(*)
          FROM pg_stat_activity
         WHERE datname = 'social'
           AND pid <> pg_backend_pid()
           AND wait_event_type = 'Lock'),
       (SELECT count(*)
          FROM pg_stat_activity
         WHERE datname = 'social'
           AND pid <> pg_backend_pid()
           AND cardinality(pg_blocking_pids(pid)) > 0),
       (SELECT count(*)
          FROM pg_locks
         WHERE NOT granted
           AND relation = 'posts'::regclass),
       (SELECT count(*)
          FROM pg_locks
         WHERE NOT granted
           AND relation = 'likes'::regclass);
SELECT 'lockevent',
       extract(epoch FROM now())::bigint,
       coalesce(wait_event, 'none'),
       count(*)
FROM pg_stat_activity
WHERE datname = 'social'
  AND pid <> pg_backend_pid()
  AND wait_event_type = 'Lock'
GROUP BY 1, 2, 3;
SELECT 'ungranted',
       extract(epoch FROM now())::bigint,
       relation::regclass::text,
       locktype,
       mode,
       coalesce(page::text, ''),
       coalesce(tuple::text, '')
FROM pg_locks
WHERE NOT granted
  AND relation IN ('posts'::regclass, 'likes'::regclass);
