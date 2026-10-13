SELECT extract(epoch FROM now())::bigint,
       coalesce(wait_event_type, 'none'),
       coalesce(wait_event, 'none'),
       count(*)
FROM pg_stat_activity
WHERE datname = 'social'
  AND state = 'active'
  AND pid <> pg_backend_pid()
GROUP BY 1, 2, 3;
