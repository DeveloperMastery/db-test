#!/usr/bin/env python3
"""Measure one Postgres database's QPS limit.

Open-loop runs last 30 seconds longer than the scored window. Percentiles and
the error rate ignore that first 30 seconds. Latency is pgbench's logged time
alone. Pass criteria are p95 < 100 ms, p99 < 500 ms, and failed+skipped < 1%.
Load-generator jobs stay at 8. A run is invalid when any one core or any one
pgbench thread averages above 70% CPU.
"""

import csv
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"
CSV_PATH = ROOT / "results" / "runs.csv"
PROGRESS = ROOT / "results" / "progress.txt"
COMMANDS = ROOT / "results" / "commands.log"
KEY = ROOT / "infra" / "id_ed25519"
DB = "134.209.22.158"
LOAD = "138.68.149.186"
DB_PRIVATE = "10.130.0.3"
N_USERS = 100_000
N_POSTS = 1_000_000
CLIENTS = 50
JOBS = 8
PHASE = "phase1"
SCALE = "1M"
WORKLOAD = "mixed"
RAMP_S = 30
READ_SCRIPT_IDS = {0, 1}
MIXED_SCRIPTS = (
    ("profile.sql", 45),
    ("open_post.sql", 45),
    ("like.sql", 8),
    ("new_post.sql", 2),
)
WRITE_SCRIPTS = (
    ("like.sql", 80),
    ("new_post.sql", 20),
)
SCRIPTS = MIXED_SCRIPTS
WARMUP_MIN_S = 600
WARMUP_MAX_S = 2400
WARMUP_QPS_SPAN = 0.05
WARMUP_HIT_SPAN = 0.005

SSH = [
    "ssh",
    "-i",
    str(KEY),
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=40",
    "-o",
    "ConnectTimeout=20",
]
PROGRESS_RE = re.compile(
    r"progress:\s+([0-9.]+)\s+s,\s+([0-9.]+)\s+tps,\s+lat\s+([0-9.]+|NaN)\s+ms\s+stddev\s+([0-9.]+|NaN)"
    r"(?:,\s+lag\s+[0-9.]+\s+ms)?,\s+([0-9]+)\s+failed"
)
RAM_BYTES = 8 * 1024**3
BILLING_START = datetime(2026, 10, 5, 12, 41, 29, tzinfo=timezone.utc)
HOURLY_USD = 0.07143 + 0.25000


def log_cmd(host, cmd):
    COMMANDS.parent.mkdir(parents=True, exist_ok=True)
    with COMMANDS.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} host={host} cmd={cmd}\n")


def progress(msg):
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    with PROGRESS.open("a") as fh:
        fh.write(line + "\n")


FIREWALL_ID = "038ee535-9e49-4399-9527-a097b032e6ed"
POSTGRES_SOURCE = "10.130.0.2/32"


def public_ip():
    last = ""
    for url in ("https://ifconfig.me", "https://api.ipify.org", "https://icanhazip.com"):
        for _ in range(3):
            proc = subprocess.run(
                ["curl", "-4", "-s", "--max-time", "10", url],
                text=True,
                capture_output=True,
            )
            ip = proc.stdout.strip()
            last = ip or last
            if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip):
                return ip
            time.sleep(2)
    raise RuntimeError(f"could not read public IP: {last!r}")


def ensure_ssh_ip():
    """Keep the SSH /32 pointed at the current public address. Leave 5432 alone."""
    ip = public_ip()
    want = f"{ip}/32"
    raw = json.loads(doctl(["compute", "firewall", "get", FIREWALL_ID, "-o", "json"]))
    fw = raw[0] if isinstance(raw, list) else raw
    ssh_addrs = []
    pg_addrs = []
    for rule in fw.get("inbound_rules") or []:
        if rule.get("protocol") != "tcp":
            continue
        addrs = (rule.get("sources") or {}).get("addresses") or []
        if str(rule.get("ports")) == "22":
            ssh_addrs.extend(addrs)
        elif str(rule.get("ports")) == "5432":
            pg_addrs.extend(addrs)
    if POSTGRES_SOURCE not in pg_addrs:
        raise RuntimeError(f"refusing to edit the firewall; 5432 allows {pg_addrs}")
    for addr in pg_addrs:
        if not addr.startswith("10.130."):
            raise RuntimeError(f"refusing to edit the firewall; 5432 allows {addr}")
    if ssh_addrs == [want]:
        return ip
    if want not in ssh_addrs:
        doctl(
            [
                "compute", "firewall", "add-rules", FIREWALL_ID,
                "--inbound-rules", f"protocol:tcp,ports:22,address:{want}",
            ]
        )
    for old in ssh_addrs:
        if old != want:
            doctl(
                [
                    "compute", "firewall", "remove-rules", FIREWALL_ID,
                    "--inbound-rules", f"protocol:tcp,ports:22,address:{old}",
                ]
            )
    log_cmd(
        "firewall",
        f"ssh inbound {','.join(ssh_addrs) or 'none'} -> {want}; 5432 unchanged {POSTGRES_SOURCE}",
    )
    progress(f"firewall SSH is {want}")
    return ip


def ssh(host, script, check=True):
    log_cmd(host, script)
    proc = subprocess.run(SSH + [f"root@{host}", script], text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"ssh {host} failed ({proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def remote_job(host, script, job, timeout_s=6 * 3600, follow=None, restart=False):
    """Run a long step in a tmux session and poll until it exits.

    The session is not a child of the SSH connection, so a dropped SSH
    session leaves the step running. A later call with the same job name
    attaches to that session instead of starting a second copy.
    """
    remote = f"/var/tmp/pg-limit/jobs/{job}"
    follow_file = follow or f"{remote}/log"
    local = ROOT / "results" / "jobs" / f"{job}.sh"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(script if script.endswith("\n") else script + "\n")
    ensure_ssh_ip()
    ssh(
        host,
        "command -v tmux >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y tmux",
    )
    ssh(host, f"mkdir -p {remote}")
    state = ssh(
        host,
        f"""
set -e
if [ "{int(restart)}" = "1" ]; then
  tmux kill-session -t {job} 2>/dev/null || true
  rm -f {remote}/exit {remote}/log
fi
if [ -f {remote}/exit ] && [ "$(cat {remote}/exit)" = "0" ]; then echo DONE; exit 0; fi
if tmux has-session -t {job} 2>/dev/null; then echo RUNNING; exit 0; fi
echo START
""",
    ).stdout.strip().splitlines()[-1]
    if state == "START":
        scp_to(host, local, f"{remote}/script.sh")
        ssh(
            host,
            f"""
set -e
chmod 700 {remote}/script.sh
rm -f {remote}/exit {remote}/log
tmux new-session -d -s {job} 'bash {remote}/script.sh > {remote}/log 2>&1; echo $? > {remote}/exit'
echo STARTED
""",
        )
    progress(f"detached {job} on {host}: {state}")
    begun = time.monotonic()
    seen = 0
    delay = 2
    while True:
        if time.monotonic() - begun > timeout_s:
            raise RuntimeError(f"{job} still running after {timeout_s:.0f}s")
        time.sleep(delay)
        delay = min(delay * 1.5, 20)
        try:
            probe = ssh(
                host,
                f"""
if [ -f {remote}/exit ]; then echo STATUS DONE $(cat {remote}/exit); else
  if tmux has-session -t {job} 2>/dev/null; then echo STATUS UP; else echo STATUS DEAD; fi
fi
if [ -f {follow_file} ]; then tail -n +{seen + 1} {follow_file}; fi
""",
                check=False,
            )
        except RuntimeError as exc:
            progress(f"{job} ssh failed ({exc.__class__.__name__}); refreshing the SSH rule")
            ensure_ssh_ip()
            continue
        if probe.returncode != 0:
            progress(f"{job} poll failed; refreshing the SSH rule")
            ensure_ssh_ip()
            continue
        lines = probe.stdout.splitlines()
        if not lines:
            continue
        status = lines[0].strip()
        chunk = lines[1:]
        seen += len(chunk)
        for line in chunk:
            text = line.rstrip()
            if text and (
                "progress" in text or text.startswith("Time:") or text.startswith("STEP|")
                or "tps" in text or "ERROR" in text or "NOTICE" in text
            ):
                progress(f"{job} {text}")
        if status.startswith("STATUS DONE"):
            code = int(status.split()[-1])
            log = ssh(host, f"cat {remote}/log", check=False).stdout
            if code != 0:
                raise RuntimeError(f"{job} exited {code}\n{log[-4000:]}")
            return log
        if status == "STATUS DEAD":
            time.sleep(2)
            again = ssh(host, f"cat {remote}/exit 2>/dev/null || true", check=False).stdout.strip()
            if again == "":
                log = ssh(host, f"tail -c 4000 {remote}/log 2>/dev/null || true", check=False).stdout
                raise RuntimeError(f"{job} ended without an exit code\n{log}")


def ssh_stream(host, script, check=True):
    """Run a long remote command and print progress lines as they arrive."""
    log_cmd(host, script)
    proc = subprocess.Popen(
        SSH + [f"root@{host}", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    chunks = []
    assert proc.stdout is not None
    for line in proc.stdout:
        chunks.append(line)
        text = line.rstrip()
        if text and (
            "progress" in text or text.startswith("Time:") or "tps" in text or "ERROR" in text
        ):
            progress(text)
    code = proc.wait()
    out = "".join(chunks)
    if check and code != 0:
        raise RuntimeError(f"ssh {host} failed ({code})\n{out[-4000:]}")
    return out


def scp_to(host, local, remote):
    log_cmd(host, f"scp {local} {remote}")
    proc = subprocess.run(
        [
            "scp",
            "-i",
            str(KEY),
            "-o",
            "StrictHostKeyChecking=accept-new",
            str(local),
            f"root@{host}:{remote}",
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return proc


def scp_from(host, remote, local):
    log_cmd(host, f"scp -r {remote} {local}")
    local.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "scp",
            "-i",
            str(KEY),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-r",
            f"root@{host}:{remote}",
            str(local),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc


def psql_db(sql):
    script = "sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At " + shlex.quote("-c " + sql)
    # The line above would pass one argument starting with -c. Build it safely.
    script = (
        "sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At <<'SQL'\n" + sql + "\nSQL"
    )
    return ssh(DB, script).stdout


def hot_lo(n):
    """First id of the newest 20% of a 1-based contiguous id range."""
    return n - n // 5 + 1


def write_sql(mode="zipfian_count"):
    """Write pgbench scripts.

    zipfian_count: original scripts, live count(*).
    zipfian_counter: same zipfian picks, reads and the like write use posts.like_count.
    eighty_twenty: 80% of picks are uniform in the newest 20% of ids, 20% are uniform over all ids.
    """
    if mode not in {"zipfian_count", "zipfian_counter", "eighty_twenty"}:
        raise ValueError(mode)
    post_lo = hot_lo(N_POSTS)
    user_lo = hot_lo(N_USERS)
    if mode == "eighty_twenty":
        user_pick = f"""\\set user_coin random(1, 100)
\\if :user_coin <= 80
\\set uid random({user_lo}, {N_USERS})
\\else
\\set uid random(1, {N_USERS})
\\endif
"""
        post_pick = f"""\\set post_coin random(1, 100)
\\if :post_coin <= 80
\\set pid random({post_lo}, {N_POSTS})
\\else
\\set pid random(1, {N_POSTS})
\\endif
"""
    else:
        user_pick = f"\\set uid random_zipfian(1, {N_USERS}, 1.5)\n"
        post_pick = f"""\\set raw random_zipfian(1, {N_POSTS}, 1.5)
\\set pid {N_POSTS} - :raw + 1
"""
    if mode == "zipfian_count":
        read_cols = """SELECT p.id, p.body, p.created_at,
       (SELECT count(*) FROM likes l WHERE l.post_id = p.id) AS like_count
FROM posts p
"""
        like = post_pick + f"""\\set uid random(1, {N_USERS})
INSERT INTO likes (post_id, user_id, created_at)
VALUES (:pid, :uid, now())
ON CONFLICT DO NOTHING;
"""
    else:
        read_cols = """SELECT p.id, p.body, p.created_at, p.like_count
FROM posts p
"""
        # Demo B keeps the original like pick: zipfian post, uniform user.
        # The 80/20 limit applies the user rule to the like write as well.
        like_user = f"\\set uid random(1, {N_USERS})\n" if mode == "zipfian_counter" else user_pick
        like = post_pick + like_user + """WITH inserted AS (
    INSERT INTO likes (post_id, user_id, created_at)
    VALUES (:pid, :uid, now())
    ON CONFLICT DO NOTHING
    RETURNING post_id
)
UPDATE posts AS p
SET like_count = p.like_count + 1
FROM inserted AS i
WHERE p.id = i.post_id;
"""
    profile = user_pick + read_cols + """WHERE p.user_id = :uid
ORDER BY p.created_at DESC
LIMIT 20;
"""
    open_post = post_pick + read_cols + """WHERE p.id = :pid;
"""
    new_post = user_pick + """\\set len random(80, 200)
INSERT INTO posts (user_id, body, created_at)
VALUES (:uid, repeat('x', :len), now());
"""
    probe = user_pick + post_pick + "INSERT INTO access_probe (user_id, post_id) VALUES (:uid, :pid);\n"
    files = {
        "profile.sql": profile,
        "open_post.sql": open_post,
        "like.sql": like,
        "new_post.sql": new_post,
        "probe.sql": probe,
    }
    local = ROOT / "infra" / "sql"
    local.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (local / name).write_text(body)
    assert_sql(mode)
    ssh(LOAD, "mkdir -p /opt/pg-limit/sql")
    log_cmd(LOAD, "scp infra/sql/*.sql /opt/pg-limit/sql/")
    subprocess.run(
        [
            "scp",
            "-i",
            str(KEY),
            "-o",
            "StrictHostKeyChecking=accept-new",
            *[str(local / name) for name in files],
            f"root@{LOAD}:/opt/pg-limit/sql/",
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def assert_sql(mode):
    local = ROOT / "infra" / "sql"
    open_post = (local / "open_post.sql").read_text()
    like = (local / "like.sql").read_text()
    profile = (local / "profile.sql").read_text()
    new_post = (local / "new_post.sql").read_text()
    post_lo = hot_lo(N_POSTS)
    user_lo = hot_lo(N_USERS)
    if mode == "zipfian_count":
        checks = [
            "random_zipfian" in open_post and "count(*)" in open_post,
            "p.like_count" not in open_post,
            "WITH inserted" not in like and "random_zipfian" in like,
            f"random(1, {N_USERS})" in like,
            "count(*)" in profile and "random_zipfian" in new_post,
        ]
    elif mode == "zipfian_counter":
        checks = [
            "random_zipfian" in open_post and "p.like_count" in open_post and "count(*)" not in open_post,
            "WITH inserted" in like and "like_count = p.like_count + 1" in like,
            "random_zipfian" in like and f"random(1, {N_USERS})" in like,
            "p.like_count" in profile and "random_zipfian" in profile and "random_zipfian" in new_post,
        ]
    else:
        checks = [
            "post_coin" in open_post and f"random({post_lo}, {N_POSTS})" in open_post,
            "p.like_count" in open_post and "random_zipfian" not in open_post,
            "post_coin" in like and "user_coin" in like and "WITH inserted" in like,
            f"random({user_lo}, {N_USERS})" in profile and "user_coin" in new_post,
            "random_zipfian" not in (open_post + like + profile + new_post),
        ]
    if not all(checks):
        raise RuntimeError(f"sql assert failed for {mode}: {checks}")


def start_collectors(run_id, locks=False, all_pgbench=False):
    remote = f"/var/tmp/pg-limit/{run_id}"
    ssh(DB, "mkdir -p /var/tmp/pg-limit")
    scp_to(DB, ROOT / "infra" / "sql" / "sample_waits.sql", "/var/tmp/pg-limit/sample_waits.sql")
    db_script = f"""
set -e
mkdir -p {remote}
rm -f {remote}/*.pid
nohup vmstat 5 > {remote}/vmstat.txt 2>&1 & echo $! > {remote}/vmstat.pid
nohup iostat -x 5 > {remote}/iostat.txt 2>&1 & echo $! > {remote}/iostat.pid
nohup bash -c 'while true; do sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At -F, -f /var/tmp/pg-limit/sample_waits.sql; sleep 5; done' > {remote}/waits.csv 2>{remote}/waits.err & echo $! > {remote}/waits.pid
"""
    if locks:
        scp_to(DB, ROOT / "infra" / "sql" / "sample_locks.sql", "/var/tmp/pg-limit/sample_locks.sql")
        db_script += f"""
nohup bash -c 'while true; do sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At -F, -f /var/tmp/pg-limit/sample_locks.sql; sleep 5; done' > {remote}/locks.csv 2>{remote}/locks.err & echo $! > {remote}/locks.pid
"""
    load_script = f"""
set -e
mkdir -p {remote}
nohup mpstat -P ALL 5 > {remote}/mpstat.txt 2>&1 & echo $! > {remote}/mpstat.pid
nohup bash -c 'for i in $(seq 1 100); do pid=$(pgrep -n pgbench || true); [ -n "$pid" ] && break; sleep 0.2; done; if [ "{int(all_pgbench)}" = "1" ]; then exec pidstat -h -t -C pgbench 5; else exec pidstat -h -t -p "$pid" 5; fi' > {remote}/pidstat.txt 2>&1 & echo $! > {remote}/pidstat.pid
"""
    ssh(DB, db_script)
    ssh(LOAD, load_script)


def stop_collectors(run_id):
    remote = f"/var/tmp/pg-limit/{run_id}"
    script = f"""
for f in {remote}/*.pid; do
  [ -f "$f" ] || continue
  kill "$(cat "$f")" 2>/dev/null || true
done
sleep 1
"""
    ssh(DB, script, check=False)
    ssh(LOAD, script, check=False)


def snapshot(label, run_id):
    sql = r"""
SELECT 'db', blks_hit, blks_read FROM pg_stat_database WHERE datname = 'social';
COPY (
  SELECT queryid::text, calls, total_exec_time, rows,
         regexp_replace(query, E'[\\n\\r]+', ' ', 'g')
  FROM pg_stat_statements
  WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'social')
) TO STDOUT WITH (FORMAT csv);
"""
    # Split into two files. Statements query can be large; that is fine at 1M.
    db_stats = psql_db(
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';"
    )
    stmts = psql_db(
        """
COPY (
  SELECT queryid::text, calls, total_exec_time, rows,
         regexp_replace(query, E'[\\n\\r]+', ' ', 'g')
  FROM pg_stat_statements
  WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'social')
) TO STDOUT WITH (FORMAT csv);
"""
    )
    tables = psql_db(
        """
COPY (
  SELECT relname, n_tup_ins, n_tup_upd, n_tup_del
  FROM pg_stat_user_tables
  WHERE relname IN ('posts', 'likes')
) TO STDOUT WITH (FORMAT csv);
"""
    )
    dest = RAW / run_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{label}_db_stats.txt").write_text(db_stats)
    (dest / f"{label}_statements.csv").write_text(stmts)
    (dest / f"{label}_tables.csv").write_text(tables)


def parse_db_stats(text):
    line = text.strip().splitlines()[-1]
    hit, read = line.split(",")
    return int(hit), int(read)


def parse_summary(text):
    def grab(pattern, default=None, cast=float):
        match = re.search(pattern, text)
        if not match:
            return default
        return cast(match.group(1))

    return {
        "processed": grab(r"number of transactions actually processed:\s+(\d+)", 0, int),
        "failed": grab(r"number of failed transactions:\s+(\d+)", 0, int),
        "skipped": grab(r"number of transactions skipped:\s+(\d+)", 0, int),
        "duration_s": grab(r"duration:\s+(\d+)\s+s", None, int),
        "tps": grab(r"tps = ([0-9.]+)", None, float),
    }


def iter_txlog(paths, script_offset=0):
    """Yield (ts, script, time_us, lag_us, ok).

    In pgbench 18 the logged time is measured from the scheduled start, so it
    already includes schedule_lag. ok is false when the latency field is not a
    number (a failed or skipped transaction).
    """
    for path in paths:
        with path.open(errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 6:
                    continue
                try:
                    script_no = int(parts[3]) + script_offset
                    ts = float(parts[4]) + float(parts[5]) / 1_000_000.0
                except ValueError:
                    continue
                try:
                    time_us = float(parts[2])
                except ValueError:
                    yield ts, script_no, None, 0.0, False
                    continue
                lag = 0.0
                if len(parts) >= 7:
                    try:
                        lag = float(parts[6])
                    except ValueError:
                        lag = 0.0
                yield ts, script_no, time_us, lag, True


def score_txlogs(paths, ramp_s, read_scripts):
    """Split a pgbench log at ramp_s after the first timestamp.

    Pass/fail latency is pgbench's logged time. That field already includes
    schedule lag, so lag is not added again. The scored window is everything
    at or after the cut. The ramp slice is kept so its p99 can be reported.
    """
    t0 = None
    saw = False
    for ts, _script, _time_us, _lag, _good in iter_txlog(paths):
        saw = True
        if t0 is None or ts < t0:
            t0 = ts
    if not saw:
        return None
    cut = t0 + ramp_s
    scored_lat = []
    scored_exec = []
    ramp_lat = []
    ramp_lag = []
    ramp_exec = []
    scored_counts = {}
    ramp_counts = {}
    scored_fail = ramp_fail = 0
    for ts, script_no, time_us, lag, good in iter_txlog(paths):
        in_ramp = ts < cut
        if not good:
            if in_ramp:
                ramp_fail += 1
            else:
                scored_fail += 1
            continue
        if in_ramp:
            ramp_lat.append(time_us)
            ramp_lag.append(lag)
            ramp_exec.append(time_us - lag)
            ramp_counts[script_no] = ramp_counts.get(script_no, 0) + 1
        else:
            scored_lat.append(time_us)
            scored_exec.append(time_us - lag)
            scored_counts[script_no] = scored_counts.get(script_no, 0) + 1

    def pack(latencies, counts, n_fail, lags=None, execs=None):
        latencies.sort()
        n_ok = len(latencies)
        denom = n_ok + n_fail
        out = {
            "n_ok": n_ok,
            "n_fail": n_fail,
            "error_rate": (n_fail / denom) if denom else None,
            "counts": counts,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        }
        if lags is not None:
            lags.sort()
            out["lag_p99"] = percentile(lags, 99)
        if execs is not None:
            execs.sort()
            out["exec_p99"] = percentile(execs, 99)
        return out

    return {
        "t0": t0,
        "ramp": pack(ramp_lat, ramp_counts, ramp_fail, ramp_lag, ramp_exec),
        "scored": pack(scored_lat, scored_counts, scored_fail, None, scored_exec),
    }


def qps_from_counts(total_qps, counts, read_scripts):
    log_n = sum(counts.values())
    if not log_n or total_qps is None:
        return None, None
    read_n = sum(counts.get(script, 0) for script in read_scripts)
    read_qps = total_qps * read_n / log_n
    write_qps = total_qps - read_qps
    return read_qps, write_qps


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    rank = math.ceil(p / 100.0 * len(sorted_vals)) - 1
    rank = min(max(rank, 0), len(sorted_vals) - 1)
    return sorted_vals[rank] / 1000.0


def avg_vmstat(text):
    header = None
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if "us" in parts and "wa" in parts and "st" in parts:
            header = parts
            continue
        if header and parts[0].isdigit():
            rows.append(parts)
    if header is None or len(rows) < 2:
        return None
    # Drop the since-boot sample.
    rows = rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    us = [float(r[idx["us"]]) for r in rows]
    sy = [float(r[idx["sy"]]) for r in rows]
    wa = [float(r[idx["wa"]]) for r in rows]
    st = [float(r[idx["st"]]) for r in rows]
    return {
        "cpu": statistics.fmean(a + b for a, b in zip(us, sy)),
        "iowait": statistics.fmean(wa),
        "steal": statistics.fmean(st),
        "samples": len(rows),
    }


def avg_mpstat(text):
    samples = parse_mpstat(text)
    totals = [sample["all"] for sample in samples if "all" in sample]
    if not totals:
        return None
    return statistics.fmean(totals)


def parse_mpstat(text):
    """Per-interval CPU busy percent, keyed by 'all' or a core id.

    The first sample is the since-boot average and is dropped.
    """
    samples = []
    current_ts = None
    current = {}
    saw_header = False
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if "%idle" in parts and "CPU" in parts:
            saw_header = True
            continue
        if not saw_header or len(parts) < 4:
            continue
        ts, cpu = parts[0], parts[1]
        try:
            idle = float(parts[-1])
        except ValueError:
            continue
        if ts != current_ts:
            if current:
                samples.append(current)
            current_ts = ts
            current = {}
        current[cpu] = 100.0 - idle
    if current:
        samples.append(current)
    if len(samples) >= 2:
        samples = samples[1:]
    return samples


def parse_pidstat(text):
    """Mean %CPU of each pgbench thread. The process total line is skipped."""
    per_tid = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 11 or parts[0] in {"#", "Linux", "Average:"}:
            continue
        if not parts[0][0].isdigit() or parts[3] == "-" or not parts[3].isdigit():
            continue
        try:
            cpu = float(parts[8])
        except ValueError:
            continue
        per_tid.setdefault(parts[3], []).append(cpu)
    means = {}
    for tid, values in per_tid.items():
        if len(values) >= 2:
            values = values[1:]
        if values:
            means[tid] = statistics.fmean(values)
    return means


def loadgen_pressure(mpstat_text, pidstat_text):
    samples = parse_mpstat(mpstat_text)
    core_means = {}
    for sample in samples:
        for cpu, busy in sample.items():
            if cpu == "all":
                continue
            core_means.setdefault(cpu, []).append(busy)
    core_avg = {cpu: statistics.fmean(values) for cpu, values in core_means.items() if values}
    thread_avg = parse_pidstat(pidstat_text or "")
    machine = [sample["all"] for sample in samples if "all" in sample]
    return {
        "machine_avg": statistics.fmean(machine) if machine else None,
        "core_avg": core_avg,
        "core_max": max(core_avg.values()) if core_avg else None,
        "thread_avg": thread_avg,
        "thread_max": max(thread_avg.values()) if thread_avg else None,
    }


def avg_iostat(text):
    header = None
    awaits = []
    utils = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Device" and "%util" in parts:
            header = parts
            continue
        if header and parts[0] in {"vda", "sda", "nvme0n1"} and "%util" in header:
            utils.append(float(parts[header.index("%util")]))
            if "r_await" in header and "w_await" in header and "r/s" in header and "w/s" in header:
                reads = float(parts[header.index("r/s")])
                writes = float(parts[header.index("w/s")])
                if reads + writes == 0:
                    awaits.append(0.0)
                else:
                    awaits.append(
                        (reads * float(parts[header.index("r_await")]) + writes * float(parts[header.index("w_await")]))
                        / (reads + writes)
                    )
            elif "await" in header:
                awaits.append(float(parts[header.index("await")]))
    if len(utils) < 2:
        return None
    utils = utils[1:]
    awaits = awaits[1:] if awaits else []
    return {
        "util": statistics.fmean(utils),
        "await_ms": statistics.fmean(awaits) if awaits else None,
        "samples": len(utils),
    }


def run_pgbench(run_id, seconds, rate, scripts=None, progress=10, log=True):
    remote = f"/var/tmp/pg-limit/{run_id}"
    rate_arg = f"--rate={rate}" if rate is not None else ""
    chosen = scripts or SCRIPTS
    files = " \\\n  ".join(f"-f /opt/pg-limit/sql/{name}@{weight}" for name, weight in chosen)
    log_arg = f"--log --log-prefix={remote}/txlog" if log else ""
    script = f"""
set -e
mkdir -p {remote}
rm -f {remote}/txlog*
stdbuf -oL -eL pgbench -n -h {DB_PRIVATE} -p 5432 -U bench -d social \\
  -M prepared -c {CLIENTS} -j {JOBS} \\
  {files} \\
  -T {seconds} -P {progress} {rate_arg} \\
  {log_arg} \\
  > {remote}/summary.txt 2>&1
"""
    remote_job(LOAD, script, f"pgbench-{run_id}", timeout_s=seconds + 900, follow=f"{remote}/summary.txt")


def run_probe():
    psql_db(
        "DROP TABLE IF EXISTS zipfian_probe; "
        "CREATE TABLE zipfian_probe (user_id bigint, post_id bigint); "
        "ALTER TABLE zipfian_probe OWNER TO bench;"
    )
    script = f"""
set -e
pgbench -n -h {DB_PRIVATE} -p 5432 -U bench -d social -M prepared -c 10 -j 4 -T 10 \\
  -f /opt/pg-limit/sql/probe.sql > /tmp/zipfian-probe-summary.txt 2>&1
"""
    ssh(LOAD, script)
    out = psql_db(
        f"""
COPY (
  SELECT 'user' AS kind, width_bucket(user_id, 1, {N_USERS + 1}, 10) AS bucket, count(*)
  FROM zipfian_probe GROUP BY 2
  UNION ALL
  SELECT 'post', width_bucket(post_id, 1, {N_POSTS + 1}, 10), count(*)
  FROM zipfian_probe GROUP BY 2
  ORDER BY 1, 2
) TO STDOUT WITH (FORMAT csv);
"""
    )
    psql_db("DROP TABLE zipfian_probe;")
    path = ROOT / "results" / "zipfian-histogram.csv"
    path.write_text("kind,bucket,count\n" + out)
    return out


def explain():
    sql = """
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, p.created_at,
       (SELECT count(*) FROM likes l WHERE l.post_id = p.id) AS like_count
FROM posts p
WHERE p.user_id = 1
ORDER BY p.created_at DESC
LIMIT 20;
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, p.created_at,
       (SELECT count(*) FROM likes l WHERE l.post_id = p.id) AS like_count
FROM posts p
WHERE p.id = 1000000;
"""
    script = "sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'\n" + sql + "\nSQL"
    text = ssh(DB, script).stdout
    (ROOT / "results" / "explain-1m.txt").write_text(text)
    return text


def append_csv(row):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.exists()
    fields = [
        "phase",
        "scale",
        "workload",
        "run_type",
        "target_rate",
        "total_qps",
        "read_qps",
        "write_qps",
        "p50",
        "p95",
        "p99",
        "error_rate",
        "pass_fail",
        "avg_cpu",
        "avg_iowait",
        "avg_steal",
        "cache_hit_ratio",
        "invalid",
        "run_id",
        "loadgen_cpu",
        "notes",
    ]
    with CSV_PATH.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow(row)


def measure(run_id, run_type, seconds, rate, slo=True, locks=False, hot=False, use_ramp=None):
    dest = RAW / run_id
    if (dest / "result.json").exists():
        progress(f"reusing finished {run_id}")
        return json.loads((dest / "result.json").read_text())
    if dest.exists():
        raise RuntimeError(f"refusing to overwrite {dest}")
    dest.mkdir(parents=True)
    if use_ramp is None:
        ramp = RAMP_S if rate is not None else 0
    else:
        ramp = RAMP_S if use_ramp else 0
    run_seconds = seconds + ramp
    progress(f"start {run_id} type={run_type} seconds={seconds} ramp={ramp} rate={rate}")
    hot_before = hottest_column() if hot else None
    snapshot("before", run_id)
    start_collectors(run_id, locks=locks)
    try:
        run_pgbench(run_id, run_seconds, rate)
    finally:
        stop_collectors(run_id)
        snapshot("after", run_id)
        if hot:
            (dest / "hottest.txt").write_text(
                "before\n" + hot_before + "\nafter\n" + hottest_column() + "\n"
            )
    scp_from(DB, f"/var/tmp/pg-limit/{run_id}", dest / "db")
    scp_from(LOAD, f"/var/tmp/pg-limit/{run_id}", dest / "loadgen")
    return finalize(dest, run_id, run_type, seconds, rate, slo=slo, ramp=ramp)


def write_intervals(dest, summary):
    rows = []
    for line in summary.splitlines():
        match = PROGRESS_RE.search(line)
        if not match:
            continue
        rows.append(
            {
                "elapsed_s": match.group(1),
                "tps": match.group(2),
                "avg_latency_ms": match.group(3),
                "stddev_ms": match.group(4),
                "failed": match.group(5),
            }
        )
    if not rows:
        return []
    path = dest / "intervals.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["elapsed_s", "tps", "avg_latency_ms", "stddev_ms", "failed"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def finalize(dest, run_id, run_type, seconds, rate, slo=True, ramp=0):
    summary_path = next((dest / "loadgen").rglob("summary.txt"))
    summary = summary_path.read_text(errors="replace")
    intervals = write_intervals(dest, summary)
    log_dir = summary_path.parent
    parsed = parse_summary(summary)
    logs = sorted(p for p in log_dir.glob("txlog*") if p.is_file())
    scored_pack = None
    ramp_pack = None
    if logs:
        split = score_txlogs(logs, ramp, READ_SCRIPT_IDS)
        if split:
            scored_pack = split["scored"]
            ramp_pack = split["ramp"] if ramp else None
    if scored_pack and ramp:
        duration = seconds
        total_qps = scored_pack["n_ok"] / duration if duration else None
        read_qps, write_qps = qps_from_counts(total_qps, scored_pack["counts"], READ_SCRIPT_IDS)
        error_rate = scored_pack["error_rate"]
        p50 = scored_pack["p50"]
        p95 = scored_pack["p95"]
        p99 = scored_pack["p99"]
    else:
        duration = parsed["duration_s"] or seconds
        if parsed["tps"] is not None:
            total_qps = parsed["tps"]
        else:
            total_qps = (parsed["processed"] / duration) if duration else None
        counts = scored_pack["counts"] if scored_pack else {}
        if counts and total_qps is not None:
            read_qps, write_qps = qps_from_counts(total_qps, counts, READ_SCRIPT_IDS)
        else:
            read_qps = write_qps = None
        denom = parsed["processed"] + parsed["failed"] + parsed["skipped"]
        error_rate = (parsed["failed"] + parsed["skipped"]) / denom if denom else None
        p50 = scored_pack["p50"] if scored_pack else None
        p95 = scored_pack["p95"] if scored_pack else None
        p99 = scored_pack["p99"] if scored_pack else None
    vm = avg_vmstat(next((dest / "db").rglob("vmstat.txt")).read_text(errors="replace"))
    mp_text = next((dest / "loadgen").rglob("mpstat.txt")).read_text(errors="replace")
    pid_path = next((dest / "loadgen").rglob("pidstat.txt"), None)
    pid_text = pid_path.read_text(errors="replace") if pid_path else ""
    pressure = loadgen_pressure(mp_text, pid_text)
    mp = pressure["machine_avg"]
    io = avg_iostat(next((dest / "db").rglob("iostat.txt")).read_text(errors="replace"))
    before = parse_db_stats((dest / "before_db_stats.txt").read_text())
    after = parse_db_stats((dest / "after_db_stats.txt").read_text())
    d_hit = after[0] - before[0]
    d_read = after[1] - before[1]
    hit_ratio = d_hit / (d_hit + d_read) if (d_hit + d_read) else None
    notes = []
    invalid = False
    if vm is None:
        invalid = True
        notes.append("missing vmstat")
    if mp is None:
        invalid = True
        notes.append("missing mpstat")
    if vm and vm["steal"] > 5:
        invalid = True
        notes.append(f"steal {vm['steal']:.2f}% > 5")
    if pressure["core_max"] is None:
        invalid = True
        notes.append("missing per-core mpstat")
    elif pressure["core_max"] > 70:
        invalid = True
        hot_core = max(pressure["core_avg"], key=pressure["core_avg"].get)
        notes.append(f"loadgen core {hot_core} {pressure['core_max']:.2f}% > 70")
    if not pressure["thread_avg"]:
        invalid = True
        notes.append("missing pgbench thread cpu")
    elif pressure["thread_max"] > 70:
        invalid = True
        hot_tid = max(pressure["thread_avg"], key=pressure["thread_avg"].get)
        notes.append(f"pgbench thread {hot_tid} {pressure['thread_max']:.2f}% > 70")
    if pressure["core_max"] is not None:
        notes.append(f"loadgen_core_max {pressure['core_max']:.2f}")
    if pressure["thread_max"] is not None:
        notes.append(f"pgbench_thread_max {pressure['thread_max']:.2f}")
    passed = (
        slo
        and not invalid
        and p95 is not None
        and p99 is not None
        and error_rate is not None
        and p95 < 100
        and p99 < 500
        and error_rate < 0.01
    )
    if not slo and not invalid:
        label = "demo"
    elif invalid:
        label = "invalid"
    elif passed:
        label = "pass"
    else:
        label = "fail"
    if ramp and ramp_pack:
        notes.append(
            f"ramp_{ramp}s_p99 {none_num(ramp_pack['p99'])} "
            f"ramp_lag_p99 {none_num(ramp_pack.get('lag_p99'))} "
            f"ramp_exec_p99 {none_num(ramp_pack.get('exec_p99'))} "
            f"ramp_fail {ramp_pack['n_fail']}"
        )
    if io:
        notes.append(f"disk_util {io['util']:.2f} await_ms {io['await_ms']}")
    row = {
        "phase": PHASE,
        "scale": SCALE,
        "workload": WORKLOAD,
        "run_type": run_type,
        "target_rate": "" if rate is None else rate,
        "total_qps": none_num(total_qps),
        "read_qps": none_num(read_qps),
        "write_qps": none_num(write_qps),
        "p50": none_num(p50),
        "p95": none_num(p95),
        "p99": none_num(p99),
        "error_rate": none_num(error_rate, 6),
        "pass_fail": label,
        "avg_cpu": none_num(vm["cpu"] if vm else None),
        "avg_iowait": none_num(vm["iowait"] if vm else None),
        "avg_steal": none_num(vm["steal"] if vm else None),
        "cache_hit_ratio": none_num(hit_ratio, 6),
        "invalid": "yes" if invalid else "no",
        "run_id": run_id,
        "loadgen_cpu": none_num(mp),
        "notes": "; ".join(notes),
    }
    append_csv(row)
    result = {
        "row": row,
        "passed": passed,
        "invalid": invalid,
        "rate": rate,
        "total_qps": total_qps,
        "io": io,
        "vm": vm,
        "hit_ratio": hit_ratio,
        "p95": p95,
        "p99": p99,
        "error_rate": error_rate,
        "summary_tail": "\n".join(summary.splitlines()[-25:]),
        "intervals": len(intervals),
        "ramp_s": ramp,
        "ramp_p99": None if not ramp_pack else ramp_pack["p99"],
        "ramp_lag_p99": None if not ramp_pack else ramp_pack.get("lag_p99"),
        "ramp_exec_p99": None if not ramp_pack else ramp_pack.get("exec_p99"),
        "scored_exec_p99": None if not scored_pack else scored_pack.get("exec_p99"),
        "loadgen_core_max": pressure["core_max"],
        "loadgen_thread_max": pressure["thread_max"],
        "ramp_n_ok": None if not ramp_pack else ramp_pack["n_ok"],
        "ramp_n_fail": None if not ramp_pack else ramp_pack["n_fail"],
        "scored_n_ok": None if not scored_pack else scored_pack["n_ok"],
        "scored_n_fail": None if not scored_pack else scored_pack["n_fail"],
    }
    (dest / "result.json").write_text(json.dumps(result, indent=2, default=str))
    ramp_note = ""
    if ramp and ramp_pack:
        ramp_note = f" ramp_p99={none_num(ramp_pack['p99'])} ramp_lag_p99={none_num(ramp_pack.get('lag_p99'))} ramp_exec_p99={none_num(ramp_pack.get('exec_p99'))}"
    progress(
        f"done {run_id} {row['pass_fail']} qps={row['total_qps']} p95={row['p95']} p99={row['p99']} err={row['error_rate']} steal={row['avg_steal']} loadgen_cpu={row['loadgen_cpu']} core_max={none_num(pressure['core_max'])} thread_max={none_num(pressure['thread_max'])}{ramp_note}"
    )
    return result


def none_num(value, digits=3):
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def measure_with_rerun(run_id, run_type, seconds, rate, attempts=3, slo=True, locks=False, hot=False, use_ramp=None):
    last = None
    for attempt in range(1, attempts + 1):
        suffix = "" if attempt == 1 else f"-rerun{attempt}"
        last = measure(
            f"{run_id}{suffix}",
            run_type,
            seconds,
            rate,
            slo=slo,
            locks=locks,
            hot=hot,
            use_ramp=use_ramp,
        )
        if not last["invalid"]:
            return last
        progress(f"{run_id} invalid, attempt {attempt}")
    return last


def search(ceiling_qps, prefix="1m-mixed", hot=False, floor_rate=0):
    lo = 0
    hi = max(1, int(math.floor(ceiling_qps)))
    best = None
    failures = []
    step = 0
    if floor_rate:
        step += 1
        anchored = measure_with_rerun(f"{prefix}-search-{step:02d}", "search", 120, int(floor_rate), hot=hot)
        if anchored["invalid"] or not anchored["passed"]:
            progress(f"floor rate {floor_rate} did not pass; not searching upward")
            failures.append(anchored)
            return None, failures
        best = anchored
        lo = int(floor_rate)
        if lo > hi:
            hi = lo
    while hi - lo > max(1, 0.05 * hi):
        mid = (lo + hi) // 2
        if mid <= lo or mid >= hi:
            break
        step += 1
        result = measure_with_rerun(f"{prefix}-search-{step:02d}", "search", 120, mid, hot=hot)
        if result["invalid"]:
            progress(f"search rate {mid} stayed invalid; treating as fail")
            failures.append(result)
            hi = mid
            continue
        if result["passed"]:
            best = result
            lo = mid
        else:
            failures.append(result)
            hi = mid
    return best, failures


def confirm(rate, prefix="1m-mixed", max_stepdowns=None, hot=False):
    current = int(rate)
    stepdowns = 0
    attempts = []
    while current >= 1:
        results = []
        slo_fail = False
        for i in (1, 2, 3):
            result = measure_with_rerun(
                f"{prefix}-confirm-r{current}-{i}", "confirm", 300, current, hot=hot
            )
            results.append(result)
            if result["invalid"] or not result["passed"]:
                slo_fail = True
                break
        ok = not slo_fail and len(results) == 3 and all(r["passed"] and not r["invalid"] for r in results)
        attempts.append({"rate": current, "passed": ok, "results": results})
        if ok:
            return current, results, attempts
        if max_stepdowns is not None and stepdowns >= max_stepdowns:
            progress(f"confirm step-down limit {max_stepdowns} reached at {current}")
            return None, results, attempts
        nxt = int(math.floor(current * 0.95))
        if nxt >= current:
            nxt = current - 1
        progress(f"confirm at {current} failed; stepping down to {nxt}")
        current = nxt
        stepdowns += 1
    return None, [], attempts


def set_autovacuum(on):
    value = "on" if on else "off"
    psql_db(f"ALTER SYSTEM SET autovacuum = {value};")
    psql_db("SELECT pg_reload_conf();")
    shown = psql_db("SHOW autovacuum;").strip()
    if shown != value:
        raise RuntimeError(f"autovacuum is {shown}, expected {value}")
    return shown


def wal_and_db_bytes():
    text = psql_db(
        """
SELECT pg_database_size('social')::text || ',' ||
       coalesce((SELECT sum(size) FROM pg_ls_waldir()), 0)::text;
"""
    ).strip()
    db_bytes, wal_bytes = text.split(",")
    return int(db_bytes), int(wal_bytes)


def hottest_likes():
    return psql_db(
        """
WITH c AS (
    SELECT post_id, count(*)::bigint AS n FROM likes GROUP BY post_id
), m AS (
    SELECT max(n) AS max_n FROM c
)
SELECT 'max=' || m.max_n
    || ' tied=' || count(*) FILTER (WHERE c.n = m.max_n)
    || ' top=' || (
        SELECT string_agg(post_id::text || ':' || n::text, ',' ORDER BY n DESC, post_id DESC)
        FROM (SELECT post_id, n FROM c ORDER BY n DESC, post_id DESC LIMIT 3) t
    )
FROM c
CROSS JOIN m
GROUP BY m.max_n;
"""
    ).strip()


def hottest_column():
    return psql_db(
        """
SELECT 'max=' || max(like_count)
    || ' tied=' || count(*) FILTER (WHERE like_count = (SELECT max(like_count) FROM posts))
FROM posts;
WITH top AS MATERIALIZED (
    SELECT id, like_count
    FROM posts
    ORDER BY like_count DESC, id DESC
    LIMIT 3
)
SELECT id::text || ' like_count=' || like_count::text
    || ' likes_rows=' || (SELECT count(*) FROM likes l WHERE l.post_id = top.id)::text
FROM top;
"""
    ).strip()


def relation_sizes():
    return psql_db(
        """
SELECT c.relname || ',' || pg_relation_size(c.oid) || ',' ||
       pg_indexes_size(c.oid) || ',' || pg_total_relation_size(c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname IN ('users', 'posts', 'likes')
ORDER BY 1;
SELECT 'database,' || pg_database_size('social');
"""
    ).strip()


def set_scale(n_posts):
    global N_POSTS, N_USERS, SCALE
    N_POSTS = n_posts
    N_USERS = n_posts // 10
    if n_posts == 1_000_000:
        SCALE = "1M"
    elif n_posts == 10_000_000:
        SCALE = "10M"
    elif n_posts == 100_000_000:
        SCALE = "100M"
    else:
        SCALE = str(n_posts)


def reload_1m(like_count, label, n_posts=1_000_000):
    set_scale(n_posts)
    progress(f"reload {SCALE} like_count={like_count} label={label}")
    db_bytes, wal_bytes = wal_and_db_bytes()
    if db_bytes + wal_bytes > 120 * 1024**3:
        raise RuntimeError(f"database+wal {db_bytes + wal_bytes} bytes is over 120GB; stopping")
    set_autovacuum(False)
    psql_db(
        """
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_pkey;
ALTER TABLE posts DROP CONSTRAINT IF EXISTS posts_pkey;
ALTER TABLE likes DROP CONSTRAINT IF EXISTS likes_pkey;
DROP INDEX IF EXISTS posts_user_created_idx;
"""
    )
    if like_count:
        psql_db(
            "ALTER TABLE posts ADD COLUMN IF NOT EXISTS like_count integer NOT NULL DEFAULT 0;"
        )
    else:
        psql_db("ALTER TABLE posts DROP COLUMN IF EXISTS like_count;")
    psql_db("TRUNCATE users, posts, likes RESTART IDENTITY;")
    # New posts omit id. The column has to be an identity so those inserts get the next value.
    psql_db(
        """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'posts'::regclass
          AND attname = 'id'
          AND attidentity IN ('d', 'a')
    ) THEN
        ALTER TABLE posts ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
    END IF;
END $$;
"""
    )
    scp_to(LOAD, ROOT / "infra" / "load_data.py", "/opt/pg-limit/load_data.py")
    copy_out = remote_job(
        LOAD,
        f"python3 /opt/pg-limit/load_data.py --host {DB_PRIVATE} --start-post 1 --end-post {n_posts} --batch 100000",
        f"load-{label}",
        timeout_s=8 * 3600,
        restart=True,
    )
    backfill = ""
    if like_count:
        # The loader writes exactly two likes per post, so the counter is 2.
        # The check below still counts the likes table.
        backfill = remote_job(
            DB,
            """sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\\timing on
SET work_mem = '1GB';
UPDATE posts SET like_count = 2;
SQL""",
            f"backfill-{label}",
            timeout_s=6 * 3600,
            restart=True,
        )
        check = psql_db(
            f"""
SELECT min(like_count)::text || ',' || max(like_count)::text || ',' ||
       sum(like_count)::bigint::text || ',' ||
       count(*) FILTER (WHERE like_count <> 2)::text || ',' ||
       (SELECT count(*) FROM likes)::text
FROM posts;
"""
        ).strip()
        expected_likes = n_posts * 2
        if check != f"2,2,{expected_likes},0,{expected_likes}":
            raise RuntimeError(f"like_count backfill check failed: {check}")
    index_out = remote_job(
        DB,
        """sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\\timing on
ALTER TABLE users ADD PRIMARY KEY (id);
ALTER TABLE posts ADD PRIMARY KEY (id);
ALTER TABLE likes ADD PRIMARY KEY (post_id, user_id);
CREATE INDEX posts_user_created_idx ON posts (user_id, created_at DESC);
SELECT setval(pg_get_serial_sequence('posts', 'id'), (SELECT max(id) FROM posts));
VACUUM ANALYZE users, posts, likes;
CHECKPOINT;
SQL""",
        f"index-{label}",
        timeout_s=8 * 3600,
        restart=True,
    )
    counts = psql_db(
        """
SELECT 'users|' || count(*) FROM users
UNION ALL SELECT 'posts|' || count(*) FROM posts
UNION ALL SELECT 'likes|' || count(*) FROM likes;
"""
    ).strip()
    got = dict(line.split("|") for line in counts.splitlines())
    expected = {
        "users": str(n_posts // 10),
        "posts": str(n_posts),
        "likes": str(n_posts * 2),
    }
    if got != expected:
        raise RuntimeError(f"unexpected counts after reload: {got}")
    bodies = psql_db("SELECT min(length(body))::text || ',' || max(length(body))::text FROM posts;").strip()
    if bodies != "80,200":
        raise RuntimeError(f"body lengths {bodies}")
    last_value = psql_db(
        "SELECT pg_sequence_last_value(pg_get_serial_sequence('posts','id')::regclass);"
    ).strip()
    if last_value != str(n_posts):
        raise RuntimeError(f"posts identity last_value {last_value}")
    set_autovacuum(True)
    db_bytes, wal_bytes = wal_and_db_bytes()
    sizes = relation_sizes()
    column = psql_db(
        """
SELECT coalesce(data_type, 'absent')
FROM (
    SELECT data_type FROM information_schema.columns
    WHERE table_name = 'posts' AND column_name = 'like_count'
) s
UNION ALL
SELECT 'absent'
WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'posts' AND column_name = 'like_count'
);
"""
    ).strip()
    text = "\n".join(
        [
            f"label {label}",
            f"like_count_column {column}",
            "COPY",
            copy_out.rstrip(),
            "BACKFILL",
            backfill.rstrip(),
            "INDEX",
            index_out.rstrip(),
            "COUNTS",
            counts,
            f"body_lengths {bodies}",
            f"identity_last {last_value}",
            "SIZES",
            sizes,
            f"wal_bytes {wal_bytes}",
            f"size_vs_ram {db_bytes / RAM_BYTES:.6f}",
            "",
        ]
    )
    path = ROOT / "results" / f"load-{label}.txt"
    path.write_text(text)
    progress(f"reload done {label} db_bytes={db_bytes} wal_bytes={wal_bytes} size_vs_ram={db_bytes / RAM_BYTES:.6f}")
    if db_bytes + wal_bytes > 120 * 1024**3:
        raise RuntimeError(f"database+wal {db_bytes + wal_bytes} bytes is over 120GB; stopping")
    return {"db_bytes": db_bytes, "wal_bytes": wal_bytes, "path": str(path)}


def summarize_locks(path):
    if not path.exists():
        return None
    samples = []
    events = {}
    ungranted = {"posts": 0, "likes": 0}
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "summary" and len(parts) >= 6:
            try:
                samples.append(
                    {
                        "lock_waits": int(parts[2]),
                        "blocked": int(parts[3]),
                        "ungranted_posts": int(parts[4]),
                        "ungranted_likes": int(parts[5]),
                    }
                )
            except ValueError:
                continue
        elif kind == "lockevent" and len(parts) >= 4:
            try:
                events[parts[2]] = max(events.get(parts[2], 0), int(parts[3]))
            except ValueError:
                continue
        elif kind == "ungranted" and len(parts) >= 3:
            ungranted[parts[2]] = ungranted.get(parts[2], 0) + 1
    if not samples:
        return {"samples": 0}
    return {
        "samples": len(samples),
        "samples_with_lock_wait": sum(1 for s in samples if s["lock_waits"] > 0),
        "samples_with_blocked": sum(1 for s in samples if s["blocked"] > 0),
        "samples_with_ungranted_posts": sum(1 for s in samples if s["ungranted_posts"] > 0),
        "samples_with_ungranted_likes": sum(1 for s in samples if s["ungranted_likes"] > 0),
        "max_lock_waits": max(s["lock_waits"] for s in samples),
        "max_blocked": max(s["blocked"] for s in samples),
        "max_ungranted_posts": max(s["ungranted_posts"] for s in samples),
        "max_ungranted_likes": max(s["ungranted_likes"] for s in samples),
        "max_lockevent_waiters": events,
        "ungranted_rows": ungranted,
    }


def run_demo(run_id, mode, workload, like_column):
    global WORKLOAD
    WORKLOAD = workload
    write_sql(mode)
    before = hottest_column() if like_column else hottest_likes()
    (ROOT / "results" / f"{run_id}-hottest-before.txt").write_text(before + "\n")
    progress(f"{run_id} hottest before: {before}")
    result = measure_with_rerun(
        run_id,
        "demo",
        600,
        None,
        attempts=3,
        slo=False,
        locks=(mode == "zipfian_counter"),
        hot=like_column,
    )
    if result["invalid"]:
        raise RuntimeError(f"{run_id} stayed invalid after reruns: {result['row']}")
    used = result["row"]["run_id"]
    after = hottest_column() if like_column else hottest_likes()
    (ROOT / "results" / f"{used}-hottest-after.txt").write_text(after + "\n")
    (RAW / used / "hottest-outside.txt").write_text("before\n" + before + "\nafter\n" + after + "\n")
    progress(f"{run_id} hottest after: {after}")
    locks = None
    if mode == "zipfian_counter":
        lock_path = next((RAW / used).rglob("locks.csv"), None)
        locks = summarize_locks(lock_path) if lock_path else {"missing": True}
        (ROOT / "results" / "demo-b-locks.json").write_text(json.dumps(locks, indent=2))
        progress(f"demo B locks: {locks}")
    return {"result": result, "before": before, "after": after, "locks": locks, "run_id": used}


def run_access_probe(tag=None):
    write_sql("eighty_twenty")
    read_check = ssh(
        LOAD,
        f"""
set -e
pgbench -n -h {DB_PRIVATE} -p 5432 -U bench -d social -M prepared -c 2 -j 2 -T 2 \\
  -f /opt/pg-limit/sql/profile.sql -f /opt/pg-limit/sql/open_post.sql \\
  > /tmp/pg-limit-if.out 2>&1
tail -n 20 /tmp/pg-limit-if.out
""",
    ).stdout
    if "tps =" not in read_check:
        raise RuntimeError(f"80/20 read script failed:\n{read_check}")
    progress("80/20 read scripts ran")
    psql_db(
        "DROP TABLE IF EXISTS access_probe; "
        "CREATE TABLE access_probe (user_id bigint, post_id bigint); "
        "ALTER TABLE access_probe OWNER TO bench;"
    )
    ssh(
        LOAD,
        f"""
set -e
pgbench -n -h {DB_PRIVATE} -p 5432 -U bench -d social -M prepared -c 10 -j 4 -T 10 \\
  -f /opt/pg-limit/sql/probe.sql > /tmp/access-probe-summary.txt 2>&1
""",
    )
    post_lo = hot_lo(N_POSTS)
    user_lo = hot_lo(N_USERS)
    out = psql_db(
        f"""
COPY (
  SELECT 'user' AS kind, width_bucket(user_id, 1, {N_USERS + 1}, 10) AS bucket, count(*)
  FROM access_probe GROUP BY 2
  UNION ALL
  SELECT 'post', width_bucket(post_id, 1, {N_POSTS + 1}, 10), count(*)
  FROM access_probe GROUP BY 2
  ORDER BY 1, 2
) TO STDOUT WITH (FORMAT csv);
"""
    )
    totals = psql_db(
        f"""
SELECT 'post_hot|' || count(*) FILTER (WHERE post_id >= {post_lo})
    || '|post_cold|' || count(*) FILTER (WHERE post_id < {post_lo})
    || '|user_hot|' || count(*) FILTER (WHERE user_id >= {user_lo})
    || '|user_cold|' || count(*) FILTER (WHERE user_id < {user_lo})
    || '|n|' || count(*)
FROM access_probe;
"""
    ).strip()
    psql_db("DROP TABLE access_probe;")
    tag = tag or SCALE
    path = ROOT / "results" / f"access-8020-histogram-{tag}.csv"
    path.write_text("kind,bucket,count\n" + out + "\n# " + totals + "\n")
    (ROOT / "results" / f"access-8020-totals-{tag}.txt").write_text(totals + "\n")
    progress(f"80/20 histogram {totals}")
    return totals


def explain_counter():
    sql = """
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, p.created_at, p.like_count
FROM posts p
WHERE p.user_id = 80001
ORDER BY p.created_at DESC
LIMIT 20;
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.id, p.body, p.created_at, p.like_count
FROM posts p
WHERE p.id = 1000000;
"""
    text = ssh(DB, "sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'\n" + sql + "\nSQL").stdout
    (ROOT / "results" / "explain-1m-8020.txt").write_text(text)
    return text


def statement_deltas(run_id):
    dest = RAW / run_id

    def load(path):
        rows = {}
        with path.open(newline="") as fh:
            for parts in csv.reader(fh):
                if len(parts) < 5:
                    continue
                try:
                    rows[parts[0]] = (int(float(parts[1])), float(parts[2]), parts[4])
                except ValueError:
                    continue
        return rows

    before = load(dest / "before_statements.csv")
    after = load(dest / "after_statements.csv")
    out = []
    for qid, (calls, total, query) in after.items():
        bc, bt, _ = before.get(qid, (0, 0.0, ""))
        dc = calls - bc
        if dc <= 0:
            continue
        out.append({"calls": dc, "mean_ms": (total - bt) / dc, "query": " ".join(query.split())[:240]})
    out.sort(key=lambda row: row["calls"], reverse=True)
    return out


def classify_statement(query):
    q = query.lower()
    if "update posts" in q and "like_count" in q:
        return "like_write"
    if "insert into posts" in q:
        return "new_post"
    if "insert into likes" in q and "update" not in q:
        return "like_insert"
    if "order by" in q and "posts" in q:
        return "profile"
    if "where p.id" in q or "where id =" in q:
        return "open_post"
    return "other"


def preflight():
    ensure_ssh_ip()
    db = ssh(DB, "echo DB_OK; uptime").stdout.strip()
    load = ssh(LOAD, "echo LOAD_OK; systemctl is-active postgresql || true").stdout.strip()
    progress(db)
    progress(load)
    if "LOAD_OK" not in load:
        raise RuntimeError(load)
    if re.search(r"(?m)^active$", load):
        raise RuntimeError("load generator local postgres is active")
    running = ssh(LOAD, "pgrep -a pgbench || true").stdout.strip()
    if running:
        raise RuntimeError(f"pgbench already running: {running}")


def run_limit_8020():
    global WORKLOAD
    WORKLOAD = "mixed-8020"
    reload_1m(True, "1m-8020")
    test_like_cte_rollback()
    run_access_probe()
    explain_counter()
    progress("warmup 600s")
    measure("1m-8020-warmup", "warmup", 600, None, hot=True)
    progress("ceiling 120s")
    ceiling = measure_with_rerun("1m-8020-ceiling", "ceiling", 120, None, attempts=2, hot=True)
    if not ceiling["total_qps"]:
        progress("ceiling produced no qps; stopping")
        return None
    progress(f"binary search under ceiling {ceiling['total_qps']:.1f}")
    best, failures = search(ceiling["total_qps"], prefix="1m-8020", hot=True)
    summary = {
        "ceiling_qps": ceiling["total_qps"],
        "ceiling": ceiling["row"],
        "candidate_rate": None if best is None else best["rate"],
        "candidate": None if best is None else best["row"],
        "confirmed_rate": None,
        "confirms": [],
        "attempts": [],
        "search_failures": [f["row"] for f in failures],
        "stopped_reason": None,
    }
    if best is None:
        summary["stopped_reason"] = "no 2-minute search rate passed"
        (ROOT / "results" / "summary-1m-8020.json").write_text(json.dumps(summary, indent=2, default=str))
        progress("no 2-minute search rate passed")
        return summary
    progress(f"confirm candidate {best['rate']} with at most 5 step-downs")
    rate, _confirms, attempts = confirm(best["rate"], prefix="1m-8020", max_stepdowns=5, hot=True)
    summary["confirmed_rate"] = rate
    summary["attempts"] = [
        {"rate": a["rate"], "passed": a["passed"], "rows": [r["row"] for r in a["results"]]}
        for a in attempts
    ]
    if rate is None:
        summary["stopped_reason"] = "5-minute confirms failed and step-down limit of 5 was reached"
    drift_rows = []
    interesting = []
    if best is not None:
        interesting.append(best["row"]["run_id"])
    for attempt in attempts:
        for result in attempt["results"]:
            interesting.append(result["row"]["run_id"])
    for run_id in interesting:
        means = {}
        for row in statement_deltas(run_id):
            kind = classify_statement(row["query"])
            if kind not in means:
                means[kind] = {"calls": row["calls"], "mean_ms": row["mean_ms"], "query": row["query"]}
        hot_path = RAW / run_id / "hottest.txt"
        drift_rows.append(
            {
                "run_id": run_id,
                "means": means,
                "hottest": hot_path.read_text() if hot_path.exists() else "",
            }
        )
    summary["drift"] = drift_rows
    end_size = wal_and_db_bytes()
    summary["end_db_bytes"] = end_size[0]
    summary["end_wal_bytes"] = end_size[1]
    summary["end_counts"] = psql_db(
        """
SELECT 'users|' || count(*) FROM users
UNION ALL SELECT 'posts|' || count(*) FROM posts
UNION ALL SELECT 'likes|' || count(*) FROM likes;
"""
    ).strip()
    (ROOT / "results" / "summary-1m-8020.json").write_text(json.dumps(summary, indent=2, default=str))
    progress(f"8020 limit finished confirmed={rate}")
    return summary


def test_like_cte_rollback():
    before = psql_db("SELECT like_count::text || ',' || (SELECT count(*) FROM likes WHERE post_id = 1000000 AND user_id = 3)::text FROM posts WHERE id = 1000000;").strip()
    psql_db(
        """
BEGIN;
WITH inserted AS (
    INSERT INTO likes (post_id, user_id, created_at)
    VALUES (1000000, 3, now())
    ON CONFLICT DO NOTHING
    RETURNING post_id
)
UPDATE posts AS p
SET like_count = p.like_count + 1
FROM inserted AS i
WHERE p.id = i.post_id;
ROLLBACK;
"""
    )
    after = psql_db("SELECT like_count::text || ',' || (SELECT count(*) FROM likes WHERE post_id = 1000000 AND user_id = 3)::text FROM posts WHERE id = 1000000;").strip()
    if before != after:
        raise RuntimeError(f"like CTE rollback changed the row: {before} -> {after}")
    progress(f"like CTE rollback left post 1000000 at {after}")


def spend_from_events(rows):
    now = datetime.now(timezone.utc)
    total = 0.0
    for index, row in enumerate(rows):
        start = datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        end = now
        if index + 1 < len(rows):
            end = datetime.strptime(rows[index + 1]["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        end = min(end, now)
        if end > start:
            total += (end - start).total_seconds() / 3600.0 * float(row["hourly"])
    return total


def spend_usd():
    events_path = ROOT / "results" / "billing-events.json"
    if events_path.exists():
        return spend_from_events(json.loads(events_path.read_text()))
    now = datetime.now(timezone.utc)
    switch_path = ROOT / "results" / "billing-switch.json"
    if not switch_path.exists():
        hours = (now - BILLING_START).total_seconds() / 3600
        return hours * HOURLY_USD
    switch = json.loads(switch_path.read_text())
    changed = datetime.strptime(switch["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    old_hours = max(0, (min(now, changed) - BILLING_START).total_seconds() / 3600)
    new_hours = max(0, (now - changed).total_seconds() / 3600)
    return old_hours * switch["old_hourly"] + new_hours * switch["new_hourly"]


def guard_spend(where):
    spent = spend_usd()
    progress(f"spend ${spent:.2f} at {where}")
    if spent >= 58:
        raise RuntimeError(f"spend ${spent:.2f} is on track to pass $60; stopping before {where}")
    return spent


def table_counts():
    log = remote_job(
        DB,
        """
sudo -u postgres psql -d social -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT 'users|' || count(*) FROM users
UNION ALL SELECT 'posts|' || count(*) FROM posts
UNION ALL SELECT 'likes|' || count(*) FROM likes;
SQL
""",
        f"table-counts-{int(time.time())}",
        timeout_s=7200,
        restart=True,
    )
    lines = [
        line.strip()
        for line in log.splitlines()
        if line.startswith(("users|", "posts|", "likes|"))
    ]
    return "\n".join(lines)


def drift_for(run_ids):
    rows = []
    for run_id in run_ids:
        means = {}
        for row in statement_deltas(run_id):
            kind = classify_statement(row["query"])
            if kind not in means:
                means[kind] = {"calls": row["calls"], "mean_ms": row["mean_ms"]}
        hot_path = RAW / run_id / "hottest.txt"
        result_path = RAW / run_id / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        rows.append(
            {
                "run_id": run_id,
                "means": means,
                "hottest": hot_path.read_text() if hot_path.exists() else "",
                "p95": result.get("p95"),
                "p99": result.get("p99"),
                "ramp_p99": result.get("ramp_p99"),
                "ramp_lag_p99": result.get("ramp_lag_p99"),
                "ramp_exec_p99": result.get("ramp_exec_p99"),
                "scored_exec_p99": result.get("scored_exec_p99"),
                "total_qps": result.get("total_qps"),
                "hit_ratio": result.get("hit_ratio"),
                "row": result.get("row"),
            }
        )
    return rows


def adaptive_warmup(run_id):
    """Closed-loop warmup until three 60s windows are stable, after 10 minutes.

    Stable means the QPS relative range is at most 5% and the cache-hit
    absolute range is at most 0.005. Stop at 40 minutes either way.
    """
    dest = RAW / run_id
    if dest.exists():
        raise RuntimeError(f"refusing to overwrite {dest}")
    dest.mkdir(parents=True)
    remote = f"/var/tmp/pg-limit/{run_id}"
    progress(f"adaptive warmup {run_id} min {WARMUP_MIN_S}s max {WARMUP_MAX_S}s")
    snapshot("before", run_id)
    start_collectors(run_id)
    files = " ".join(f"-f /opt/pg-limit/sql/{name}@{weight}" for name, weight in SCRIPTS)
    base = psql_db(
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';"
    ).strip()
    prev_hit, prev_read = [int(part) for part in base.split(",")]
    ssh(
        LOAD,
        f"""
set -e
mkdir -p {remote}
command -v tmux >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y tmux
tmux kill-session -t {run_id} 2>/dev/null || true
tmux new-session -d -s {run_id} 'stdbuf -oL -eL pgbench -n -h {DB_PRIVATE} -p 5432 -U bench -d social -M prepared -c {CLIENTS} -j {JOBS} {files} -T {WARMUP_MAX_S} -P 60 > {remote}/summary.txt 2>&1'
sleep 1
tmux has-session -t {run_id}
""",
    )
    started = time.monotonic()
    windows = []
    seen = 0
    stable = False
    capped = False
    try:
        target = 65
        while True:
            elapsed = time.monotonic() - started
            if spend_usd() >= 58:
                raise RuntimeError(f"spend ${spend_usd():.2f} is on track to pass $60 during {run_id}")
            wait = target - elapsed
            if wait > 0:
                time.sleep(min(wait, 30))
                continue
            summary = ssh(LOAD, f"cat {remote}/summary.txt 2>/dev/null || true", check=False).stdout
            matches = [PROGRESS_RE.search(line) for line in summary.splitlines()]
            matches = [match for match in matches if match]
            if len(matches) <= seen:
                alive = ssh(
                    LOAD,
                    f"if tmux has-session -t {run_id} 2>/dev/null; then echo UP; else echo DOWN; fi",
                    check=False,
                ).stdout
                if "DOWN" in alive and elapsed < WARMUP_MAX_S - 5:
                    raise RuntimeError(f"warmup pgbench exited early at {elapsed:.0f}s\n{summary[-2000:]}")
                if elapsed >= WARMUP_MAX_S + 30:
                    capped = True
                    break
                time.sleep(10)
                continue
            stats = psql_db(
                "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';"
            ).strip()
            hit, read = [int(part) for part in stats.split(",")]
            new = matches[seen:]
            for index, match in enumerate(new):
                last = index == len(new) - 1
                if last and prev_hit is not None:
                    d_hit = hit - prev_hit
                    d_read = read - prev_read
                    interval_hit = d_hit / (d_hit + d_read) if (d_hit + d_read) else None
                else:
                    interval_hit = None
                windows.append(
                    {
                        "elapsed_s": float(match.group(1)),
                        "qps": float(match.group(2)),
                        "avg_latency_ms": None if match.group(3) == "NaN" else float(match.group(3)),
                        "failed": int(match.group(5)),
                        "hit_ratio": interval_hit,
                    }
                )
            prev_hit, prev_read = hit, read
            seen = len(matches)
            target += 60 * len(new)
            last = windows[-1]
            progress(
                f"warmup {run_id} t={last['elapsed_s']:.0f}s qps={last['qps']:.1f} hit={last['hit_ratio']}"
            )
            if last["elapsed_s"] >= WARMUP_MIN_S and len(windows) >= 3:
                tail = windows[-3:]
                if all(window["hit_ratio"] is not None for window in tail):
                    qps = [window["qps"] for window in tail]
                    hits = [window["hit_ratio"] for window in tail]
                    qmean = sum(qps) / 3
                    qspan = (max(qps) - min(qps)) / qmean if qmean else 1
                    hspan = max(hits) - min(hits)
                    progress(f"warmup span qps={qspan:.4f} hit={hspan:.6f}")
                    if qspan <= WARMUP_QPS_SPAN and hspan <= WARMUP_HIT_SPAN:
                        stable = True
                        break
            if last["elapsed_s"] >= WARMUP_MAX_S:
                capped = True
                break
    finally:
        ssh(LOAD, f"tmux kill-session -t {run_id} 2>/dev/null || true", check=False)
        stop_collectors(run_id)
        snapshot("after", run_id)
    scp_from(DB, remote, dest / "db")
    scp_from(LOAD, remote, dest / "loadgen")
    elapsed_s = time.monotonic() - started
    info = {
        "mode": "adaptive",
        "elapsed_s": elapsed_s,
        "stable": stable,
        "capped": capped,
        "qps_span_limit": WARMUP_QPS_SPAN,
        "hit_span_limit": WARMUP_HIT_SPAN,
        "min_s": WARMUP_MIN_S,
        "max_s": WARMUP_MAX_S,
        "windows": windows,
    }
    (dest / "warmup.json").write_text(json.dumps(info, indent=2))
    vm = avg_vmstat(next((dest / "db").rglob("vmstat.txt")).read_text(errors="replace"))
    mp_text = next((dest / "loadgen").rglob("mpstat.txt")).read_text(errors="replace")
    pid_path = next((dest / "loadgen").rglob("pidstat.txt"), None)
    pressure = loadgen_pressure(mp_text, pid_path.read_text(errors="replace") if pid_path else "")
    mp = pressure["machine_avg"]
    before = parse_db_stats((dest / "before_db_stats.txt").read_text())
    after = parse_db_stats((dest / "after_db_stats.txt").read_text())
    d_hit = after[0] - before[0]
    d_read = after[1] - before[1]
    hit_ratio = d_hit / (d_hit + d_read) if (d_hit + d_read) else None
    tail = windows[-3:] if len(windows) >= 3 else windows
    qmean = sum(window["qps"] for window in tail) / len(tail) if tail else None
    notes = [
        f"warmup_s {elapsed_s:.0f}",
        f"stable {stable}",
        f"capped {capped}",
        f"loadgen_core_max {none_num(pressure['core_max'])}",
        f"pgbench_thread_max {none_num(pressure['thread_max'])}",
        "windows " + ",".join(f"{window['qps']:.1f}/{window['hit_ratio']}" for window in tail),
    ]
    append_csv(
        {
            "phase": PHASE,
            "scale": SCALE,
            "workload": WORKLOAD,
            "run_type": "warmup",
            "target_rate": "",
            "total_qps": none_num(qmean),
            "read_qps": "",
            "write_qps": "",
            "p50": "",
            "p95": "",
            "p99": "",
            "error_rate": "",
            "pass_fail": "warmup",
            "avg_cpu": none_num(vm["cpu"] if vm else None),
            "avg_iowait": none_num(vm["iowait"] if vm else None),
            "avg_steal": none_num(vm["steal"] if vm else None),
            "cache_hit_ratio": none_num(hit_ratio, 6),
            "invalid": "yes" if (vm and vm["steal"] > 5) or (pressure["core_max"] or 0) > 70 or (pressure["thread_max"] or 0) > 70 else "no",
            "run_id": run_id,
            "loadgen_cpu": none_num(mp),
            "notes": "; ".join(notes),
        }
    )
    progress(f"warmup done {run_id} seconds={elapsed_s:.0f} stable={stable} capped={capped}")
    return info


def run_open_limit(prefix, floor_rate=None, warmup="fixed"):
    write_sql("eighty_twenty")
    if warmup == "fixed":
        measure(f"{prefix}-warmup", "warmup", 600, None, hot=True)
        warmup_info = {"mode": "fixed", "elapsed_s": 600, "stable": True, "capped": False, "windows": []}
    else:
        warmup_info = adaptive_warmup(f"{prefix}-warmup")
    progress(f"{prefix} ceiling 120s")
    ceiling = measure_with_rerun(f"{prefix}-ceiling", "ceiling", 120, None, attempts=2, hot=True)
    summary = {
        "prefix": prefix,
        "scale": SCALE,
        "workload": WORKLOAD,
        "scripts": list(SCRIPTS),
        "warmup": warmup_info,
        "floor_rate": floor_rate,
        "ceiling_qps": None if ceiling is None else ceiling["total_qps"],
        "ceiling": None if ceiling is None else ceiling["row"],
        "candidate_rate": None,
        "candidate": None,
        "confirmed_rate": None,
        "attempts": [],
        "search_failures": [],
        "stopped_reason": None,
        "drift": [],
    }
    if ceiling is None or not ceiling["total_qps"]:
        summary["stopped_reason"] = "ceiling produced no qps"
        return summary
    progress(f"{prefix} search under ceiling {ceiling['total_qps']:.1f} floor {floor_rate}")
    best, failures = search(ceiling["total_qps"], prefix=prefix, hot=True, floor_rate=floor_rate or 0)
    summary["candidate_rate"] = None if best is None else best["rate"]
    summary["candidate"] = None if best is None else best["row"]
    summary["search_failures"] = [item["row"] for item in failures]
    interesting = [item["row"]["run_id"] for item in failures]
    if best is not None:
        interesting.append(best["row"]["run_id"])
    if best is None:
        summary["stopped_reason"] = "no 2-minute search rate passed"
        summary["drift"] = drift_for(interesting)
        return summary
    progress(f"{prefix} confirm {best['rate']} with at most 5 step-downs")
    rate, _confirms, attempts = confirm(best["rate"], prefix=prefix, max_stepdowns=5, hot=True)
    summary["confirmed_rate"] = rate
    summary["attempts"] = [
        {"rate": attempt["rate"], "passed": attempt["passed"], "rows": [item["row"] for item in attempt["results"]]}
        for attempt in attempts
    ]
    for attempt in attempts:
        for result in attempt["results"]:
            interesting.append(result["row"]["run_id"])
    if rate is None:
        summary["stopped_reason"] = "5-minute confirms failed and step-down limit of 5 was reached"
    summary["drift"] = drift_for(interesting)
    summary["end_db_bytes"], summary["end_wal_bytes"] = wal_and_db_bytes()
    summary["end_counts"] = table_counts()
    return summary


def run_phase1():
    ckpt_path = ROOT / "results" / "phase1-checkpoint.txt"
    done = set(ckpt_path.read_text().split()) if ckpt_path.exists() else set()
    if "stopped" in done:
        progress(f"phase1 already stopped: {ckpt_path.read_text().strip()}")
        return
    stages = (
        ("1m-ramp", 1_000_000, "mixed", 13919, "fixed"),
        ("10m", 10_000_000, "mixed", None, "adaptive"),
        ("100m", 100_000_000, "mixed", None, "adaptive"),
        ("100m-write", 100_000_000, "write", None, "adaptive"),
    )
    for name, n_posts, workload, floor, warmup in stages:
        if name in done:
            progress(f"skip completed {name}")
            continue
        guard_spend(name)
        if workload == "mixed":
            global_workload("mixed")
            reload_1m(True, name, n_posts=n_posts)
            if name == "1m-ramp":
                test_like_cte_rollback()
            run_access_probe(name)
        else:
            global_workload("write")
            set_scale(n_posts)
            counts = table_counts()
            (ROOT / "results" / "write-only-start-counts.txt").write_text(counts + "\n")
            progress(f"write-only starts from {counts}")
        summary = run_open_limit(name, floor_rate=floor, warmup=warmup)
        (ROOT / "results" / f"summary-{name}.json").write_text(json.dumps(summary, indent=2, default=str))
        progress(f"{name} confirmed={summary['confirmed_rate']} reason={summary['stopped_reason']}")
        if summary["stopped_reason"]:
            ckpt_path.write_text(f"stopped {name}\n")
            progress("phase1 stopped before the next scale")
            return
        done.add(name)
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    progress("phase1 finished. Stopping before Phase 2.")


def global_workload(kind):
    global WORKLOAD, SCRIPTS, READ_SCRIPT_IDS
    if kind == "mixed":
        WORKLOAD = "mixed-8020"
        SCRIPTS = MIXED_SCRIPTS
        READ_SCRIPT_IDS = {0, 1}
    elif kind == "write":
        WORKLOAD = "write-8020"
        SCRIPTS = WRITE_SCRIPTS
        READ_SCRIPT_IDS = set()
    else:
        raise ValueError(kind)


def load_state():
    global DB, DB_PRIVATE, HOURLY_USD
    vals = {}
    for line in (ROOT / "infra" / "state.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            vals[key] = value
    DB = vals["DB_PUBLIC"]
    DB_PRIVATE = vals["DB_PRIVATE"]
    HOURLY_USD = float(vals.get("DB_HOURLY", "0.07143")) + float(vals.get("LOAD_HOURLY", "0.25000"))


def execution_is_flat(summary):
    """True when the queries stayed fast across the failed confirms.

    Flat means every recorded statement mean is under 1 ms and every scored
    execution p99 is under 20 ms. Execution is logged time minus schedule lag.
    """
    execs = []
    means = []
    for row in summary.get("drift") or []:
        if row.get("scored_exec_p99") is not None:
            execs.append(row["scored_exec_p99"])
        for kind in ("open_post", "profile", "like_write", "new_post"):
            stat = (row.get("means") or {}).get(kind)
            if stat:
                means.append(stat["mean_ms"])
    if not execs or not means:
        return False
    return max(execs) < 20 and max(means) < 1


def doctl(args):
    token = ""
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith("DIGITALOCEAN_ACCESS_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        raise RuntimeError("DIGITALOCEAN_ACCESS_TOKEN is missing")
    env = os.environ.copy()
    env["DIGITALOCEAN_ACCESS_TOKEN"] = token
    log_cmd("doctl", " ".join(args))
    proc = subprocess.run(
        [str(ROOT / "infra" / "bin" / "doctl"), *args],
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"doctl {' '.join(args)} failed\n{proc.stderr[-2000:]}")
    return proc.stdout


def disk_need_gib():
    """100M estimate: 1M database size times 100, plus 8 GB WAL, both with 40% headroom."""
    data = 655_972_031 * 100 * 1.4
    wal = 8 * 1024**3 * 1.4
    return (data + wal) / 1024**3


def choose_dedicated_size():
    need = disk_need_gib()
    sizes = json.loads(doctl(["compute", "size", "list", "-o", "json"]))
    options = []
    for size in sizes:
        if size.get("vcpus") != 4 or size.get("memory") != 8192:
            continue
        if "lon1" not in (size.get("regions") or []):
            continue
        slug = size.get("slug") or ""
        desc = size.get("description") or ""
        dedicated = slug.startswith("c") and "Basic" not in desc and "Shared" not in desc
        usable = (size.get("disk") or 0) * 0.90
        options.append(
            {
                "slug": slug,
                "description": desc,
                "disk_gib": size.get("disk"),
                "usable_gib": round(usable, 1),
                "price_hourly": size.get("price_hourly"),
                "price_monthly": size.get("price_monthly"),
                "dedicated": dedicated,
                "fits": dedicated and usable >= need,
            }
        )
    (ROOT / "results" / "dedicated-options.json").write_text(json.dumps({"need_gib": need, "options": options}, indent=2))
    fits = [row for row in options if row["fits"]]
    if not fits:
        progress(f"no dedicated 4 vCPU 8 GB size has {need:.1f} GiB usable disk")
        return None, options, need
    fits.sort(key=lambda row: row["price_hourly"])
    return fits[0], options, need


def migrate_dedicated():
    chosen, _options, need = choose_dedicated_size()
    if chosen is None:
        return None
    progress(
        f"dedicated size {chosen['slug']} disk {chosen['disk_gib']} GiB "
        f"${chosen['price_hourly']}/hr, need {need:.1f} GiB"
    )
    existing = json.loads(doctl(["compute", "droplet", "list", "-o", "json"]))
    ours = [d for d in existing if "pg-limit-test" in (d.get("tags") or [])]
    found = next((d for d in ours if d.get("name") == "pg-limit-db-dedi"), None)
    if found is None:
        if len(ours) >= 3:
            raise RuntimeError("already at 3 tagged droplets; not creating another")
        created = json.loads(
            doctl(
                [
                    "compute", "droplet", "create", "pg-limit-db-dedi",
                    "--region", "lon1",
                    "--size", chosen["slug"],
                    "--image", "ubuntu-26-04-x64",
                    "--ssh-keys", "59853148",
                    "--vpc-uuid", "fd199783-a389-4502-a428-7d5af216c5b9",
                    "--tag-name", "pg-limit-test",
                    "--wait",
                    "-o", "json",
                ]
            )
        )
        found = created[0] if isinstance(created, list) else created
    droplet_id = str(found["id"])
    try:
        doctl(["compute", "firewall", "add-droplets", "038ee535-9e49-4399-9527-a097b032e6ed", "--droplet-ids", droplet_id])
    except RuntimeError as exc:
        progress(f"firewall add skipped: {exc}")
    detail = json.loads(doctl(["compute", "droplet", "get", droplet_id, "-o", "json"]))
    detail = detail[0] if isinstance(detail, list) else detail
    public = detail["networks"]["v4"]
    public_ip = next(net["ip_address"] for net in public if net["type"] == "public")
    private_ip = next(net["ip_address"] for net in public if net["type"] == "private")
    progress(f"dedicated droplet {droplet_id} public {public_ip} private {private_ip}")
    for _ in range(60):
        try:
            ssh(public_ip, "echo UP")
            break
        except RuntimeError:
            time.sleep(10)
    else:
        raise RuntimeError(f"ssh to {public_ip} did not come up")
    already = ssh(
        public_ip,
        "sudo -u postgres psql -d social -At -c 'SELECT 1' 2>/dev/null || true",
        check=False,
    ).stdout.strip()
    if already == "1":
        progress("dedicated postgres is already answering; skipping setup")
    else:
        conf = (ROOT / "infra" / "pg-limit.conf").read_text().replace("10.130.0.3", private_ip)
        conf_path = ROOT / "results" / "postgres-dedicated.conf"
        conf_path.write_text(conf)
        ssh(public_ip, "mkdir -p /root/pg-limit && chmod 700 /root/pg-limit")
        scp_to(public_ip, conf_path, "/root/pg-limit/pg-limit.conf")
        scp_to(public_ip, ROOT / "infra" / "pg_hba.conf", "/root/pg-limit/pg_hba.conf")
        scp_to(public_ip, ROOT / "infra" / "db.pass", "/root/pg-limit/db.pass")
        scp_to(public_ip, ROOT / "infra" / "setup-db.sh", "/root/pg-limit/setup-db.sh")
        ssh(public_ip, "chmod 600 /root/pg-limit/db.pass /root/pg-limit/pg_hba.conf")
        ssh_stream(public_ip, "bash /root/pg-limit/setup-db.sh")
    ssh(
        LOAD,
        f"""python3 - <<'PY'
from pathlib import Path
path = Path("/root/.pgpass")
out = []
for line in path.read_text().splitlines():
    parts = line.split(":")
    if len(parts) >= 5 and parts[2] == "social":
        parts[0] = "{private_ip}"
        out.append(":".join(parts))
    elif line:
        out.append(line)
path.write_text("\\n".join(out) + "\\n")
path.chmod(0o600)
PY""",
    )
    vals = {}
    state_path = ROOT / "infra" / "state.env"
    for line in state_path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            vals[key] = value
    if vals.get("DB_NAME") != "pg-limit-db-dedi":
        vals["OLD_DB_ID"] = vals.get("DB_ID", "")
        vals["OLD_DB_PUBLIC"] = vals.get("DB_PUBLIC", "")
        vals["OLD_DB_HOURLY"] = vals.get("DB_HOURLY", "")
    vals["DB_ID"] = droplet_id
    vals["DB_NAME"] = "pg-limit-db-dedi"
    vals["DB_PUBLIC"] = public_ip
    vals["DB_PRIVATE"] = private_ip
    vals["DB_SIZE"] = chosen["slug"]
    vals["DB_HOURLY"] = str(chosen["price_hourly"])
    state_path.write_text("".join(f"{key}={value}\n" for key, value in vals.items()))
    state_path.chmod(0o600)
    load_state()
    switched = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "old_hourly": 0.07143 + 0.25000,
        "new_hourly": float(chosen["price_hourly"]) + 0.25000,
        "size": chosen["slug"],
        "price_hourly": chosen["price_hourly"],
        "need_gib": need,
        "droplet_id": droplet_id,
    }
    switch_path = ROOT / "results" / "billing-switch.json"
    if not switch_path.exists():
        switch_path.write_text(json.dumps(switched, indent=2))
    check = ssh(public_ip, "sudo -u postgres psql -d social -At -c 'SELECT 1'").stdout.strip()
    if check != "1":
        raise RuntimeError(f"new database did not answer: {check}")
    try:
        old = json.loads(doctl(["compute", "droplet", "get", "606292659", "-o", "json"]))
        old = old[0] if isinstance(old, list) else old
    except RuntimeError:
        old = {}
    if old.get("name") == "pg-limit-db" and "pg-limit-test" in (old.get("tags") or []):
        try:
            doctl(["compute", "droplet", "delete", "606292659", "--force"])
            progress("deleted old shared-CPU database droplet 606292659 after the dedicated one answered")
        except RuntimeError as exc:
            progress(f"old droplet delete skipped: {exc}")
    progress(f"database moved to {chosen['slug']} at ${chosen['price_hourly']}/hr")
    return switched


def run_closed_sweep(prefix):
    global CLIENTS
    saved = CLIENTS
    write_sql("eighty_twenty")
    client_rows = []
    qualifying = []
    try:
        for clients in (8, 16, 32, 64, 128):
            CLIENTS = clients
            progress(f"{prefix} closed-loop -c {clients} -j {JOBS}")
            results = []
            for i in (1, 2, 3):
                result = measure_with_rerun(
                    f"{prefix}-c{clients}-{i}",
                    "closed-limit",
                    300,
                    None,
                    hot=True,
                    use_ramp=True,
                )
                results.append(result)
            qps = sorted(result["total_qps"] or 0 for result in results)
            median = qps[1]
            passed = len(results) == 3 and all(result["passed"] and not result["invalid"] for result in results)
            client_rows.append(
                {
                    "clients": clients,
                    "jobs": JOBS,
                    "passed": passed,
                    "median_qps": median,
                    "rows": [result["row"] for result in results],
                }
            )
            if passed:
                qualifying.append((median, clients))
    finally:
        CLIENTS = saved
    best = max(qualifying) if qualifying else None
    summary = {
        "prefix": prefix,
        "method": "closed-loop",
        "scale": SCALE,
        "workload": WORKLOAD,
        "clients": client_rows,
        "confirmed_rate": None if best is None else best[0],
        "confirmed_clients": None if best is None else best[1],
        "stopped_reason": None if best else "no client count passed three closed-loop runs",
    }
    summary["end_db_bytes"], summary["end_wal_bytes"] = wal_and_db_bytes()
    summary["end_counts"] = table_counts()
    return summary


def run_one_scale(name, n_posts, workload, warmup, method):
    guard_spend(name)
    if workload == "mixed":
        global_workload("mixed")
        reload_1m(True, name, n_posts=n_posts)
        if n_posts == 1_000_000:
            test_like_cte_rollback()
        run_access_probe(name)
    else:
        global_workload("write")
        set_scale(n_posts)
        counts = table_counts()
        (ROOT / "results" / "write-only-start-counts.txt").write_text(counts + "\n")
        progress(f"write-only starts from {counts}")
    if method == "open":
        summary = run_open_limit(name, floor_rate=None, warmup=warmup)
    else:
        if warmup == "fixed":
            measure(f"{name}-warmup", "warmup", 600, None, hot=True)
        else:
            adaptive_warmup(f"{name}-warmup")
        summary = run_closed_sweep(name)
    (ROOT / "results" / f"summary-{name}.json").write_text(json.dumps(summary, indent=2, default=str))
    progress(f"{name} confirmed={summary.get('confirmed_rate')} reason={summary.get('stopped_reason')}")
    return summary


def psql_steps(log):
    steps = []
    values = {}
    pending = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("STEP|"):
            pending = line.split("|", 1)[1]
            continue
        if "|" in line and not line.startswith("Time"):
            key, value = line.split("|", 1)
            values[key] = value
        match = re.match(r"Time:\s+([0-9.]+)\s+ms", line)
        if match and pending:
            steps.append((pending, float(match.group(1)) / 1000.0))
            pending = None
    return steps, values


def prepare_100m():
    """Verify the loaded 100M heap, then build indexes. Does not reload."""
    set_scale(100_000_000)
    set_autovacuum(False)
    db_bytes, wal_bytes = wal_and_db_bytes()
    if db_bytes + wal_bytes > 120 * 1024**3:
        raise RuntimeError(f"database+wal {db_bytes + wal_bytes} bytes is over 120GB; stopping")
    progress(f"100M before index db_bytes={db_bytes} wal_bytes={wal_bytes}")
    verify_log = remote_job(
        DB,
        """sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\\timing on
\\echo STEP|users
SELECT 'users|' || count(*)::text FROM users;
\\echo STEP|posts
SELECT 'posts|' || count(*)::text FROM posts;
\\echo STEP|likes
SELECT 'likes|' || count(*)::text FROM likes;
\\echo STEP|like_count
SELECT 'like_count|' || min(like_count)::text || ',' || max(like_count)::text || ',' ||
       count(*) FILTER (WHERE like_count <> 2)::text FROM posts;
\\echo STEP|bodies
SELECT 'bodies|' || min(length(body))::text || ',' || max(length(body))::text FROM posts;
SQL""",
        "100m-v2-verify",
        timeout_s=4 * 3600,
    )
    verify_steps, verify_values = psql_steps(verify_log)
    expected = {
        "users": "10000000",
        "posts": "100000000",
        "likes": "200000000",
        "like_count": "2,2,0",
        "bodies": "80,200",
    }
    if verify_values != expected:
        raise RuntimeError(f"100M data is not intact: {verify_values}")
    progress("100M row counts and like_count check passed")
    index_log = remote_job(
        DB,
        """sudo -u postgres psql -d social -v ON_ERROR_STOP=1 <<'SQL'
\\timing on
SET maintenance_work_mem = '1GB';
\\echo STEP|users_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_pkey') THEN
        ALTER TABLE users ADD PRIMARY KEY (id);
    END IF;
END $$;
\\echo STEP|posts_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'posts_pkey') THEN
        ALTER TABLE posts ADD PRIMARY KEY (id);
    END IF;
END $$;
\\echo STEP|likes_pkey
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'likes_pkey') THEN
        ALTER TABLE likes ADD PRIMARY KEY (post_id, user_id);
    END IF;
END $$;
\\echo STEP|posts_user_created_idx
CREATE INDEX IF NOT EXISTS posts_user_created_idx ON posts (user_id, created_at DESC);
\\echo STEP|setval
SELECT setval(pg_get_serial_sequence('posts', 'id'), (SELECT max(id) FROM posts));
\\echo STEP|identity
SELECT 'identity|' || pg_sequence_last_value(pg_get_serial_sequence('posts','id')::regclass)::text;
\\echo STEP|vacuum
VACUUM ANALYZE users, posts, likes;
\\echo STEP|checkpoint
CHECKPOINT;
SQL""",
        "100m-v2-index",
        timeout_s=8 * 3600,
    )
    index_steps, index_values = psql_steps(index_log)
    if index_values.get("identity") != "100000000":
        raise RuntimeError(f"posts identity last_value {index_values.get('identity')}")
    set_autovacuum(True)
    db_bytes, wal_bytes = wal_and_db_bytes()
    sizes = relation_sizes()
    lines = [
        "label 100m-v2",
        "heap was already loaded; indexes built after the dropped SSH session",
        "VERIFY",
    ]
    lines.extend(f"{name}_s {seconds:.3f}" for name, seconds in verify_steps)
    lines.append("INDEX")
    lines.extend(f"{name}_s {seconds:.3f}" for name, seconds in index_steps)
    lines.extend(
        [
            "COUNTS",
            "users|10000000",
            "posts|100000000",
            "likes|200000000",
            "like_count 2,2,0",
            "body_lengths 80,200",
            f"identity_last {index_values.get('identity')}",
            "SIZES",
            sizes,
            f"wal_bytes {wal_bytes}",
            f"size_vs_ram {db_bytes / RAM_BYTES:.6f}",
            "",
        ]
    )
    path = ROOT / "results" / "load-100m-v2.txt"
    path.write_text("\n".join(lines))
    progress(
        f"100M indexes done db_bytes={db_bytes} wal_bytes={wal_bytes} "
        f"size_vs_ram={db_bytes / RAM_BYTES:.6f}"
    )
    if db_bytes + wal_bytes > 120 * 1024**3:
        raise RuntimeError(f"database+wal {db_bytes + wal_bytes} bytes is over 120GB; stopping")
    return {"db_bytes": db_bytes, "wal_bytes": wal_bytes, "verify": verify_steps, "index": index_steps}


def resume_phase1():
    """Continue from the loaded 100M heap. 1M and 10M are already confirmed."""
    load_state()
    preflight()
    ckpt_path = ROOT / "results" / "phase1-v2-checkpoint.txt"
    done = set(ckpt_path.read_text().split()) if ckpt_path.exists() else set()
    if "stopped" in done:
        progress(f"phase1 v2 already stopped: {ckpt_path.read_text().strip()}")
        return
    if "100m-v2" not in done:
        guard_spend("100m-v2")
        prepare_100m()
        global_workload("mixed")
        run_access_probe("100m-v2")
        summary = run_open_limit("100m-v2", warmup="adaptive")
        (ROOT / "results" / "summary-100m-v2.json").write_text(
            json.dumps(summary, indent=2, default=str)
        )
        progress(f"100m-v2 confirmed={summary.get('confirmed_rate')} reason={summary.get('stopped_reason')}")
        if summary.get("stopped_reason"):
            ckpt_path.write_text("stopped 100m-v2\n")
            progress("phase1 stopped before the write-only test")
            return
        done.add("100m-v2")
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    if "100m-v2-write" not in done:
        summary = run_one_scale("100m-v2-write", 100_000_000, "write", "adaptive", "open")
        if summary.get("stopped_reason"):
            ckpt_path.write_text("stopped 100m-v2-write\n")
            progress("phase1 stopped after the write-only test failed to confirm")
            return
        done.add("100m-v2-write")
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    progress("phase1 v2 finished. Stopping before Phase 2.")


def run_from_dedicated():
    """Shared-CPU runs were invalid on steal. Move, then redo Phase 1 there."""
    load_state()
    switched = migrate_dedicated()
    if switched is None:
        (ROOT / "results" / "phase1-v2-checkpoint.txt").write_text("stopped no-dedicated-size\n")
        return
    preflight()
    ckpt_path = ROOT / "results" / "phase1-v2-checkpoint.txt"
    done = {"migrated"}
    summary = run_one_scale("1m-dedi", 1_000_000, "mixed", "fixed", "open")
    method = "open"
    if summary.get("stopped_reason"):
        if "step-down" in summary["stopped_reason"] and execution_is_flat(summary):
            progress("dedicated 1M confirms failed with flat query execution; switching limits to closed loop")
            method = "closed"
            done.add("method-closed")
            summary = run_one_scale("1m-closed", 1_000_000, "mixed", "fixed", "closed")
            if summary.get("stopped_reason"):
                ckpt_path.write_text("stopped 1m-closed\n")
                return
            done.add("1m-closed")
        else:
            ckpt_path.write_text("stopped 1m-dedi\n")
            progress(f"dedicated 1M stopped: {summary.get('stopped_reason')}")
            return
    else:
        done.add("1m-dedi")
    ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    later = (
        ("10m-v2", 10_000_000, "mixed", "adaptive"),
        ("100m-v2", 100_000_000, "mixed", "adaptive"),
        ("100m-v2-write", 100_000_000, "write", "adaptive"),
    )
    for name, n_posts, workload, warmup in later:
        summary = run_one_scale(name, n_posts, workload, warmup, method)
        if summary.get("stopped_reason"):
            ckpt_path.write_text(f"stopped {name}\n")
            progress("phase1 stopped before the next scale")
            return
        done.add(name)
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    progress("phase1 v2 finished. Stopping before Phase 2.")


def run_phase1_v2():
    load_state()
    ckpt_path = ROOT / "results" / "phase1-v2-checkpoint.txt"
    done = set(ckpt_path.read_text().split()) if ckpt_path.exists() else set()
    if "stopped" in done:
        progress(f"phase1 v2 already stopped: {ckpt_path.read_text().strip()}")
        return
    method = "closed" if "method-closed" in done else "open"
    if "1m-v2" not in done and "1m-dedi" not in done and "1m-closed" not in done:
        summary = run_one_scale("1m-v2", 1_000_000, "mixed", "fixed", "open")
        if summary.get("stopped_reason"):
            if "step-down" in summary["stopped_reason"] and execution_is_flat(summary):
                progress("1M confirms failed with flat query execution; moving the database to dedicated CPU")
                switched = migrate_dedicated()
                if switched is None:
                    ckpt_path.write_text("stopped no-dedicated-size\n")
                    return
                done.add("migrated")
                summary = run_one_scale("1m-dedi", 1_000_000, "mixed", "fixed", "open")
                if summary.get("stopped_reason"):
                    if "step-down" in summary["stopped_reason"] and execution_is_flat(summary):
                        progress("dedicated 1M confirms also failed with flat execution; switching limits to closed loop")
                        method = "closed"
                        done.add("method-closed")
                        summary = run_one_scale("1m-closed", 1_000_000, "mixed", "fixed", "closed")
                        if summary.get("stopped_reason"):
                            ckpt_path.write_text("stopped 1m-closed\n")
                            return
                        done.add("1m-closed")
                    else:
                        ckpt_path.write_text("stopped 1m-dedi\n")
                        progress(f"dedicated 1M stopped: {summary.get('stopped_reason')}")
                        return
                else:
                    done.add("1m-dedi")
            else:
                ckpt_path.write_text("stopped 1m-v2\n")
                progress(f"1M stopped: {summary.get('stopped_reason')}")
                return
        else:
            done.add("1m-v2")
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    later = (
        ("10m-v2", 10_000_000, "mixed", "adaptive"),
        ("100m-v2", 100_000_000, "mixed", "adaptive"),
        ("100m-v2-write", 100_000_000, "write", "adaptive"),
    )
    for name, n_posts, workload, warmup in later:
        if name in done:
            progress(f"skip completed {name}")
            continue
        summary = run_one_scale(name, n_posts, workload, warmup, method)
        if summary.get("stopped_reason"):
            ckpt_path.write_text(f"stopped {name}\n")
            progress("phase1 v2 stopped before the next scale")
            return
        done.add(name)
        ckpt_path.write_text(" ".join(sorted(done)) + "\n")
    progress("phase1 v2 finished. Stopping before Phase 2.")


def main():
    import sys

    stage = sys.argv[1] if len(sys.argv) > 1 else "redo-1m"
    if stage == "phase1":
        preflight()
        run_phase1_v2()
        return
    if stage == "phase1-dedi":
        load_state()
        preflight()
        run_from_dedicated()
        return
    if stage == "phase1-resume":
        resume_phase1()
        return
    if stage == "reparse-smoke":
        finalize(RAW / "1m-mixed-smoke", "1m-mixed-smoke", "smoke", 20, None)
        return
    if stage in {"redo-1m", "demo-a", "demo-b", "limit-8020"}:
        preflight()
    if stage in {"redo-1m", "demo-a"}:
        reload_1m(False, "1m-demo-a")
        run_demo("1m-demo-a", "zipfian_count", "demo-a-hotkey", like_column=False)
    if stage in {"redo-1m", "demo-b"}:
        reload_1m(True, "1m-demo-b")
        test_like_cte_rollback()
        run_demo("1m-demo-b", "zipfian_counter", "demo-b-fix", like_column=True)
    if stage in {"redo-1m", "limit-8020"}:
        run_limit_8020()
    if stage == "redo-1m":
        progress("STOP before 10M. Demos and the 1M 80/20 limit are done.")
        return
    if stage != "all":
        return
    write_sql("zipfian_count")
    progress("refusing the old all stage; use redo-1m")


if __name__ == "__main__":
    main()
