# Harness

How a limit run works, and which files do it. A new scale can start from this without repeating the failed scoring.

## What a published limit is

One pgbench transaction is one query, so transactions per second are queries per second. The published number is the offered `--rate` when three separate 5-minute runs all pass:

- p95 of logged time under 100 ms
- p99 of logged time under 500 ms
- failed plus skipped under 1%

Logged time already includes schedule lag. Do not add lag again. The first 30 seconds are excluded from those percentiles and from the error rate. QPS is scored-window successes divided by scored seconds. Users are that measured QPS times 10.

A run is invalid, and is not published, when database steal averages above 5%, any load-generator core averages above 70%, any pgbench thread averages above 70%, or the per-core or per-thread sample is missing.

## The shape of one limit

1. Warm up. At 1M this is a fixed 10 minutes. At 10M and above it is adaptive: at least 10 minutes, stop when three consecutive 60-second windows have a QPS range of at most 5% and a cache-hit range of at most 0.005, otherwise stop at 40 minutes and say so.
2. One 2-minute closed-loop ceiling. No `--rate`. Its QPS is the top of the search.
3. Binary search with 2-minute `--rate` runs until `hi - lo` is within 5% of `hi`.
4. Three 5-minute runs at the best passing rate. If one fails, drop the rate by 5% and repeat the three, at most 5 step-downs. Then stop and ask.

pgbench is always `-M prepared -n -c 50 -j 8` for a limit. The load generator has 8 vCPUs, so `-j` matches the machine.

## Workload

Mixed weights are profile 45, open post 45, like 8, new post 2. Write-only is like 80, new post 20. The 80/20 draw is in the scripts: 80% of ids are uniform in the newest fifth of the loaded ids, 20% are uniform over the whole loaded range. `hot_lo` in `infra/run_limit.py` freezes that bound. Do not recompute it from the live row count.

`infra/run_limit.py` `write_sql("eighty_twenty")` writes `infra/sql/*.sql` and copies them to the load generator.

## Where the code is

- `infra/run_limit.py` loads data, runs one primary, scores logs, and writes `results/runs.csv`. `remote_job` is the tmux runner. `measure` copies collectors into `results/raw/<run-id>/`. `score_txlogs` applies the 30-second cut.
- `infra/phase2.py` builds the replica, runs the split read/write load, drops the slot, resizes the primary with `--resize-disk=false`, retunes Postgres, and runs the single-primary limit again.
- `infra/load_data.py` copies users, posts, and likes. Pass the database private address. It does not build indexes.
- `infra/setup-db.sh` installs Postgres 18 from PGDG and applies `infra/pg-limit.conf` and `infra/pg_hba.conf`.
- `infra/state.env` is the droplet ids and addresses. `infra/db.pass` and `infra/repl.pass` are mode 600 and stay out of logs.
- `infra/bin/doctl` talks to DigitalOcean. The token is read from `.env` inside the process and is not written to `results/`.

Long steps go through `remote_job`: the script is copied to the droplet, started in a detached tmux session, and the local process polls an exit file. Warmup pgbench is killed when the warmup function returns, so it cannot overlap the next run.

## Collectors

On the database: `vmstat 5`, `iostat -x 5`, wait-event samples, and a pg_stat_database plus pg_stat_statements snapshot before and after. On the load generator: `mpstat -P ALL 5` and `pidstat -h -t` of the pgbench threads. The replica run also samples `pg_stat_replication` every 5 seconds and runs vmstat and iostat on the replica. A split run uses `pidstat -C pgbench` so both pgbench processes are visible.

## Replica split

pgbench has one host per process. The replica test starts two processes, both `-c 50 -j 8`. Reads use profile at 50 and open post at 50 against the replica, at 90% of the offered rate. Writes use like at 80 and new post at 20 against the primary, at the other 10%. The combined scored latency is the pass/fail. Drop the replication slot before leaving the replica stopped.

## Starting another scale

Set the scale with `set_scale`, call `global_workload`, and call `run_open_limit` with `warmup="adaptive"`. That path does not reload. `reload_1m` truncates. Do not call it against a database whose rows you need to keep. Do not call `phase1` or `phase1-dedi`; the first still points at a deleted address, and the second reloads 1M.

Keep Postgres on the private address. SSH is a /32 of the current public address and is refreshed by `ensure_ssh_ip`. Port 5432 is only the load generator and, during the replica test, the replica.

Raw logs live in `results/raw/<run-id>/` and are not overwritten. `results/commands.log` gets every remote command except secrets. `results/runs.csv` gets one row per run.

## Checks that keep a rerun from destroying data

- Restart Postgres with `systemctl restart postgresql@18-main`. `postgresql.service` is a dummy unit and a successful restart of it does not apply `shared_buffers`.
- `pg_basebackup` does not copy `conf.d` or `pg_hba.conf`. Install both before starting `postgresql@18-main`, and keep `max_connections` at least as high as the primary.
- `doctl` resize `--wait` can return with the droplet still `off`. Power it on. Confirm users, posts, and likes are still at least the loaded counts before retuning. Pass `--resize-disk=false`.
- Drop the replication slot before the replica stays stopped. `infra/phase2.py` skips the replica block once `results/phase2-checkpoint.txt` contains `replica-limit`. Calling `pg_basebackup` again wipes the replica data directory.
- `ensure_ssh_ip` must succeed before a long poll. It reads the public address from more than one URL. Port 5432 stays on `10.130` addresses only.
- Quote every `echo "STEP|..."` in a remote script. An unquoted `|` is a pipe.
