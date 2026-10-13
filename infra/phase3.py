#!/usr/bin/env python3
"""Resize the primary back to 8 GB, recheck 8,505, then a split-read replica.

The 1,522 write-only confirms finished before any replica existed, so this
script does not rerun write-only. It does not reload or truncate.
"""

import json
import math
import os
import shutil
import time
from pathlib import Path

import phase2 as p2
import run_limit as rl

ROOT = rl.ROOT
CKPT = ROOT / "results" / "phase3-checkpoint.txt"
DB_ID = p2.DB_ID
SMALL_SIZE = "c5-4vcpu-8gb"
SMALL_HOURLY = 0.18452
LOAD_SIZE = "c-8"
LOAD_HOURLY = 0.25
GIB = 1024**3
HOT_POSTS = 20_000_000
HOT_USERS = 2_000_000
LOADED_LIKES = 200_000_000


def done_set():
    if not CKPT.exists():
        return set()
    return set(CKPT.read_text().split())


def mark(token):
    done = done_set()
    done.add(token)
    CKPT.write_text(" ".join(sorted(done)) + "\n")


def other_phase3():
    import subprocess

    proc = subprocess.run(["pgrep", "-f", "python3 -u infra/phase3.py"], text=True, capture_output=True)
    pids = [int(part) for part in proc.stdout.split() if part.isdigit()]
    parent = os.getppid()
    return any(pid not in {os.getpid(), parent} for pid in pids)


def wait_ssh_powered(droplet_id, host):
    for _ in range(40):
        detail = p2.droplet_detail(droplet_id)
        if detail.get("status") != "active":
            rl.progress(f"{droplet_id} status {detail.get('status')}; powering on")
            rl.doctl(["compute", "droplet-action", "power-on", str(droplet_id), "--wait"])
        try:
            rl.ssh(host, "echo UP")
            return
        except RuntimeError:
            rl.ensure_ssh_ip()
            time.sleep(10)
    raise RuntimeError(f"ssh to {host} did not come up")


def postgres_up(host):
    for _ in range(30):
        try:
            if p2.psql_on(host, "SELECT 1;", database="postgres").strip() == "1":
                return
        except RuntimeError:
            rl.ssh(host, "systemctl start postgresql@18-main || true", check=False)
            time.sleep(10)
    raise RuntimeError("postgres did not start; the data directory was not modified")


def assert_counts(counts):
    parsed = dict(line.split("|", 1) for line in counts.splitlines() if "|" in line)
    users = int(parsed.get("users", "0"))
    posts = int(parsed.get("posts", "0"))
    likes = int(parsed.get("likes", "0"))
    if users < 10_000_000 or posts < 100_000_000 or likes < LOADED_LIKES:
        raise p2.StopAndAsk(f"row counts are short of the loaded 100M data: {counts}")
    return users, posts, likes


def drop_idle_slots():
    left = p2.psql_on(rl.DB, "SELECT count(*) FROM pg_replication_slots;", database="postgres").strip()
    if left == "0":
        return
    active = p2.psql_on(
        rl.DB,
        "SELECT count(*) FROM pg_replication_slots WHERE active;",
        database="postgres",
    ).strip()
    if active != "0":
        raise p2.StopAndAsk(f"{active} replication slots are active; not resizing while WAL can be pinned")
    names = p2.psql_on(
        rl.DB,
        "SELECT slot_name FROM pg_replication_slots;",
        database="postgres",
    ).split()
    for name in names:
        if not name.replace("_", "").isalnum():
            raise p2.StopAndAsk(f"unexpected slot name {name}")
        p2.psql_on(rl.DB, f"SELECT pg_drop_replication_slot('{name}');", database="postgres")
    left = p2.psql_on(rl.DB, "SELECT count(*) FROM pg_replication_slots;", database="postgres").strip()
    if left != "0":
        raise p2.StopAndAsk(f"primary still has {left} replication slots")


def apply_8gb(public, private):
    conf = (ROOT / "results" / "postgres-dedicated.conf").read_text()
    lines = []
    for line in conf.splitlines():
        if line.startswith("listen_addresses"):
            lines.append(f"listen_addresses = '{private}'")
        else:
            lines.append(line)
    local = ROOT / "results" / "postgres-8gb-restored.conf"
    local.write_text("\n".join(lines) + "\n")
    rl.ssh(public, "mkdir -p /etc/postgresql/18/main/conf.d")
    rl.scp_to(public, local, "/etc/postgresql/18/main/conf.d/pg-limit.conf")
    rl.ssh(
        public,
        """
set -e
chown postgres:postgres /etc/postgresql/18/main/conf.d/pg-limit.conf
chmod 644 /etc/postgresql/18/main/conf.d/pg-limit.conf
systemctl restart postgresql@18-main
systemctl is-active postgresql@18-main
""",
    )
    shown = p2.psql_on(
        public,
        "SELECT name || '=' || setting FROM pg_settings WHERE name = 'shared_buffers';",
    )
    if "shared_buffers=262144" not in shown.replace(" ", ""):
        raise RuntimeError(f"8 GB settings did not apply: {shown}")
    rl.progress("postgres restored to shared_buffers 2GB")


def resize_down():
    if "downsized" in done_set():
        rl.load_state()
        rl.progress("primary already resized back to c5-4vcpu-8gb")
        return
    before = p2.droplet_detail(DB_ID)
    if before.get("name") != "pg-limit-db-dedi" or "pg-limit-test" not in (before.get("tags") or []):
        raise p2.StopAndAsk("refusing to resize a droplet that is not the tagged primary")
    disk_before = p2.droplet_disk(before)
    if disk_before is None:
        raise p2.StopAndAsk("could not read the primary disk size; not resizing")
    chosen = p2.size_price(SMALL_SIZE)
    if chosen is None or int(chosen.get("disk") or 0) != disk_before:
        raise p2.StopAndAsk(
            f"{SMALL_SIZE} disk {None if chosen is None else chosen.get('disk')} does not match primary disk {disk_before}"
        )
    p2.guard("resize primary to 8 GB")
    rl.load_state()
    if before.get("status") == "active" and before.get("size_slug") != SMALL_SIZE:
        try:
            postgres_up(rl.DB)
            drop_idle_slots()
        except RuntimeError as exc:
            rl.progress(f"slot check skipped: {exc}")
    if before.get("size_slug") != SMALL_SIZE:
        rl.progress(f"resizing primary to {SMALL_SIZE} ${chosen['price_hourly']}/hr disk stays {disk_before} GB")
        rl.doctl(
            [
                "compute", "droplet-action", "resize", DB_ID,
                "--size", SMALL_SIZE,
                "--resize-disk=false",
                "--wait",
            ]
        )
    after = p2.droplet_detail(DB_ID)
    if after.get("status") != "active":
        rl.progress("primary is powered off after resize; powering on")
        rl.doctl(["compute", "droplet-action", "power-on", DB_ID, "--wait"])
        after = p2.droplet_detail(DB_ID)
    if p2.droplet_disk(after) != disk_before or after.get("size_slug") != SMALL_SIZE:
        raise p2.StopAndAsk(
            f"resize landed on size {after.get('size_slug')} disk {p2.droplet_disk(after)}; not reloading"
        )
    public, private = p2.droplet_ips(after)
    p2.update_state(
        {
            "DB_PUBLIC": public,
            "DB_PRIVATE": private,
            "DB_SIZE": SMALL_SIZE,
            "DB_HOURLY": str(chosen["price_hourly"]),
        }
    )
    wait_ssh_powered(DB_ID, public)
    postgres_up(public)
    apply_8gb(public, private)
    counts = rl.table_counts()
    (ROOT / "results" / "resize-down-counts.txt").write_text(counts + "\n")
    assert_counts(counts)
    p2.add_billing(float(chosen["price_hourly"]), "primary c5-4vcpu-8gb only, before the load generator returned")
    mark("downsized")
    rl.progress(f"primary is {SMALL_SIZE}; counts held; 8 GB settings restored")


def tagged_droplets():
    existing = json.loads(rl.doctl(["compute", "droplet", "list", "-o", "json"]))
    return [item for item in existing if "pg-limit-test" in (item.get("tags") or []) and item.get("status") != "archive"]


def create_droplet(name, size):
    found = next((item for item in tagged_droplets() if item.get("name") == name), None)
    if found is None:
        if len(tagged_droplets()) >= 3:
            raise p2.StopAndAsk("already at 3 tagged droplets; not creating another")
        p2.guard(f"create {name}")
        created = json.loads(
            rl.doctl(
                [
                    "compute", "droplet", "create", name,
                    "--region", "lon1",
                    "--size", size,
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
    if droplet_id == DB_ID:
        raise p2.StopAndAsk(f"refusing to treat the primary as {name}")
    try:
        rl.doctl(["compute", "firewall", "add-droplets", rl.FIREWALL_ID, "--droplet-ids", droplet_id])
    except RuntimeError as exc:
        rl.progress(f"firewall add skipped: {exc}")
    detail = p2.droplet_detail(droplet_id)
    public, private = p2.droplet_ips(detail)
    wait_ssh_powered(droplet_id, public)
    return droplet_id, public, private


def allow_bench(ip):
    if not ip.startswith("10.130."):
        raise p2.StopAndAsk(f"refusing to open 5432 to {ip}")
    path = ROOT / "infra" / "pg_hba.conf"
    text = path.read_text()
    if f"{ip}/32" not in text:
        path.write_text(text.rstrip() + f"\nhost    all             bench           {ip}/32           scram-sha-256\n")
    rl.scp_to(rl.DB, path, "/etc/postgresql/18/main/pg_hba.conf")
    rl.ssh(
        rl.DB,
        """
set -e
chown postgres:postgres /etc/postgresql/18/main/pg_hba.conf
chmod 640 /etc/postgresql/18/main/pg_hba.conf
sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 -c 'SELECT pg_reload_conf();'
""",
    )
    p2.allow_postgres_from(ip)


def install_loadgen(host, db_private):
    rl.ssh(host, "mkdir -p /root/pg-limit && chmod 700 /root/pg-limit")
    rl.scp_to(host, ROOT / "infra" / "db.pass", "/root/pg-limit/db.pass")
    rl.ssh(host, "chmod 600 /root/pg-limit/db.pass")
    script = f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
echo "STEP|wait-apt"
for i in $(seq 1 90); do
  if pgrep -x apt-get >/dev/null || pgrep -x apt >/dev/null || pgrep -x dpkg >/dev/null; then
    echo "STEP|apt-busy"
    sleep 10
  else
    break
  fi
done
ok=0
for i in $(seq 1 10); do
  if apt-get update -y; then
    ok=1
    break
  fi
  echo "STEP|apt-update-retry"
  sleep 15
done
test "$ok" = 1
apt-get install -y postgresql-common ca-certificates curl gnupg sysstat tmux python3
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt-get install -y postgresql-client-18 postgresql-18
systemctl disable --now postgresql@18-main || true
systemctl disable --now postgresql || true
python3 - <<'PY'
from pathlib import Path
pw = Path("/root/pg-limit/db.pass").read_text().strip()
Path("/root/.pgpass").write_text("{db_private}:5432:social:bench:" + pw + "\\n")
Path("/root/.pgpass").chmod(0o600)
PY
install -d -m 755 /opt/pg-limit/sql /var/tmp/pg-limit
pgbench --version
echo "STEP|loadgen-done"
"""
    rl.remote_job(host, script, "loadgen-packages", timeout_s=3600, restart=True)
    check = rl.ssh(host, f"psql -h {db_private} -U bench -d social -At -c 'SELECT 1'").stdout.strip()
    if check != "1":
        raise RuntimeError(f"load generator could not query the primary: {check}")


def recreate_loadgen():
    if "loadgen" in done_set():
        vals = p2.state_values()
        rl.LOAD = vals["LOAD_PUBLIC"]
        rl.load_state()
        rl.progress(f"load generator already recreated at {rl.LOAD}")
        return
    droplet_id, public, private = create_droplet("pg-limit-loadgen", LOAD_SIZE)
    p2.update_state(
        {
            "LOAD_ID": droplet_id,
            "LOAD_NAME": "pg-limit-loadgen",
            "LOAD_PUBLIC": public,
            "LOAD_PRIVATE": private,
            "LOAD_SIZE": LOAD_SIZE,
            "LOAD_HOURLY": f"{LOAD_HOURLY:.5f}",
            "LOAD_DESTROYED": "no",
        }
    )
    rl.LOAD = public
    allow_bench(private)
    install_loadgen(public, p2.state_values()["DB_PRIVATE"])
    rl.set_scale(100_000_000)
    rl.global_workload("mixed")
    rl.write_sql("eighty_twenty")
    p2.add_billing(SMALL_HOURLY + LOAD_HOURLY, "primary c5-4vcpu-8gb plus load generator")
    mark("loadgen")
    rl.progress(f"load generator {droplet_id} public {public} private {private}")


def capture_hot_set(users, posts, likes):
    text = p2.psql_on(
        rl.DB,
        """
SELECT 'size|' || r.relname || '|' || r.relkind::text || '|' || pg_relation_size(r.oid)
    || '|' || coalesce(t.relname, '')
FROM pg_class r
LEFT JOIN pg_index i ON i.indexrelid = r.oid
LEFT JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = r.relnamespace
WHERE n.nspname = 'public' AND r.relkind IN ('r', 'i')
ORDER BY 1;
""",
    )
    groups = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 5 or parts[0] != "size":
            continue
        _tag, name, kind, nbytes, table = parts
        key = name if kind == "r" else table
        bucket = groups.setdefault(key, {"heap": 0, "indexes": 0, "indexes_detail": {}})
        if kind == "r":
            bucket["heap"] += int(nbytes)
        else:
            bucket["indexes"] += int(nbytes)
            bucket["indexes_detail"][name] = int(nbytes)
    posts_frac = HOT_POSTS / posts
    users_frac = HOT_USERS / users
    extra_likes = max(0, likes - LOADED_LIKES)
    hot_likes = HOT_POSTS * 2 + 0.8 * extra_likes
    likes_frac = hot_likes / likes
    pieces = {
        "posts": posts_frac * (groups["posts"]["heap"] + groups["posts"]["indexes"]),
        "users": users_frac * (groups["users"]["heap"] + groups["users"]["indexes"]),
        "likes": likes_frac * (groups["likes"]["heap"] + groups["likes"]["indexes"]),
    }
    info = {
        "counts": {"users": users, "posts": posts, "likes": likes},
        "relations": groups,
        "fractions": {"posts": posts_frac, "users": users_frac, "likes": likes_frac, "hot_likes": hot_likes},
        "hot_bytes": pieces,
        "hot_total": sum(pieces.values()),
        "page_cache_8": 8 * GIB - 2 * GIB,
        "page_cache_16": 16 * GIB - 4 * GIB,
    }
    (ROOT / "results" / "hot-set.json").write_text(json.dumps(info, indent=2) + "\n")
    rl.progress(f"hot set estimate {info['hot_total'] / GIB:.2f} GiB")
    return info


def prepare_bench():
    rl.PHASE = "phase3"
    rl.set_scale(100_000_000)
    rl.global_workload("mixed")
    rl.write_sql("eighty_twenty")
    rl.load_state()
    vals = p2.state_values()
    rl.LOAD = vals["LOAD_PUBLIC"]


def sanity():
    if "sanity" in done_set():
        path = ROOT / "results" / "summary-100m-8gb-recheck.json"
        rl.progress("reusing the 8 GB recheck")
        return json.loads(path.read_text())
    prepare_bench()
    p2.guard("100m-8gb-recheck")
    counts_path = ROOT / "results" / "resize-down-counts.txt"
    counts = counts_path.read_text() if counts_path.exists() else rl.table_counts()
    users, posts, likes = assert_counts(counts)
    if not (ROOT / "results" / "hot-set.json").exists():
        capture_hot_set(users, posts, likes)
    warmup_dir = rl.RAW / "100m-8gb-recheck-warmup"
    if (warmup_dir / "warmup.json").exists():
        warmup = json.loads((warmup_dir / "warmup.json").read_text())
        rl.progress("reusing 100m-8gb-recheck-warmup")
    else:
        if warmup_dir.exists():
            shutil.rmtree(warmup_dir)
        warmup = rl.adaptive_warmup("100m-8gb-recheck-warmup")
    rl.progress("100m-8gb-recheck confirm 8505 with no step-down")
    rate, _rows, attempts = rl.confirm(8505, prefix="100m-8gb-recheck", max_stepdowns=0, hot=True)
    summary = {
        "prefix": "100m-8gb-recheck",
        "scale": "100M",
        "workload": "mixed-8020",
        "warmup": warmup,
        "stood": rate == 8505,
        "confirmed_rate": rate,
        "attempts": [
            {"rate": item["rate"], "passed": item["passed"], "rows": [row["row"] for row in item["results"]]}
            for item in attempts
        ],
        "search_failures": [],
        "stopped_reason": None,
        "end_db_bytes": None,
        "end_wal_bytes": None,
    }
    if rate != 8505:
        rl.progress("8505 did not pass; searching for a new single-box limit")
        found = rl.run_open_limit("100m-8gb-new", warmup="adaptive")
        summary["replacement"] = found
        summary["confirmed_rate"] = found.get("confirmed_rate")
        summary["stopped_reason"] = found.get("stopped_reason")
        summary["end_db_bytes"] = found.get("end_db_bytes")
        summary["end_wal_bytes"] = found.get("end_wal_bytes")
    else:
        summary["end_db_bytes"], summary["end_wal_bytes"] = rl.wal_and_db_bytes()
    (ROOT / "results" / "summary-100m-8gb-recheck.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    mark("sanity")
    rl.progress(f"8 GB recheck confirmed={summary.get('confirmed_rate')} stood={summary['stood']}")
    return summary


def triple_rates(offered):
    if offered is None:
        return None, None, None
    offered = int(offered)
    write_rate = max(1, int(round(offered * 0.10)))
    read_primary = int(round(offered * 0.45))
    read_replica = offered - write_rate - read_primary
    if read_replica < 1 or read_primary < 1:
        raise RuntimeError(f"split rates collapsed at offered {offered}")
    return read_primary, read_replica, write_rate


def wait_lag_under(limit_s):
    started = time.monotonic()
    while True:
        p2.guard("wait for replay lag")
        if not p2.streaming(rl.DB, p2.state_values()["REPLICA_PUBLIC"]):
            rl.progress("replica is not streaming yet")
            time.sleep(15)
            continue
        raw = p2.psql_on(
            rl.DB,
            "SELECT coalesce(extract(epoch from replay_lag), 0) FROM pg_stat_replication;",
            database="postgres",
        ).strip()
        try:
            lag = float(raw)
        except ValueError:
            lag = None
        rl.progress(f"replay_lag_s={raw}")
        if lag is not None and lag < limit_s:
            return lag
        if time.monotonic() - started > 3 * 3600:
            raise RuntimeError(f"replay lag stayed at {raw}s for 3 hours")
        time.sleep(15)


def measure_triple(run_id, run_type, seconds, offered, replica_host, replica_private):
    dest = rl.RAW / run_id
    if (dest / "result.json").exists():
        rl.progress(f"reusing finished {run_id}")
        return json.loads((dest / "result.json").read_text())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    ramp = rl.RAMP_S if offered is not None else 0
    run_seconds = seconds + ramp
    read_primary_rate, read_replica_rate, write_rate = triple_rates(offered)
    rl.progress(
        f"start {run_id} type={run_type} seconds={seconds} ramp={ramp} offered={offered} "
        f"read_primary={read_primary_rate} read_replica={read_replica_rate} write={write_rate}"
    )
    p2.guard(run_id)
    primary_before = rl.psql_db("SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';")
    replica_before = p2.psql_on(
        replica_host,
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
    )
    (dest / "before_primary.txt").write_text(primary_before)
    (dest / "before_replica.txt").write_text(replica_before)
    jobs = {
        "read-primary": p2.pgbench_script(run_id, "read-primary", rl.DB_PRIVATE, p2.READ_FILES, run_seconds, read_primary_rate),
        "read-replica": p2.pgbench_script(run_id, "read-replica", replica_private, p2.READ_FILES, run_seconds, read_replica_rate),
        "write": p2.pgbench_script(run_id, "write", rl.DB_PRIVATE, p2.WRITE_FILES, run_seconds, write_rate),
    }
    names = [f"p3-{run_id}-{side}" for side in jobs]
    follows = {f"p3-{run_id}-{side}": f"/var/tmp/pg-limit/{run_id}/{side}/summary.txt" for side in jobs}
    try:
        for side, script in jobs.items():
            p2.begin_job(rl.LOAD, f"p3-{run_id}-{side}", script)
        rl.start_collectors(run_id, all_pgbench=True)
        p2.start_replica_collectors(run_id, replica_host)
        p2.start_lag(run_id)
        p2.wait_jobs(rl.LOAD, names, follows, run_seconds + 900)
    finally:
        p2.kill_jobs(rl.LOAD, names)
        rl.stop_collectors(run_id)
        p2.stop_replica_collectors(run_id, replica_host)
    primary_after = rl.psql_db("SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';")
    replica_after = p2.psql_on(
        replica_host,
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
    )
    (dest / "after_primary.txt").write_text(primary_after)
    (dest / "after_replica.txt").write_text(replica_after)
    rl.scp_from(rl.DB, f"/var/tmp/pg-limit/{run_id}", dest / "db")
    rl.scp_from(rl.LOAD, f"/var/tmp/pg-limit/{run_id}", dest / "loadgen")
    rl.scp_from(replica_host, f"/var/tmp/pg-limit/{run_id}", dest / "replica")
    return finalize_triple(
        dest, run_id, run_type, seconds, offered, read_primary_rate, read_replica_rate, write_rate, ramp
    )


def finalize_triple(dest, run_id, run_type, seconds, offered, read_primary_rate, read_replica_rate, write_rate, ramp):
    sides = {}
    for side in ("read-primary", "read-replica", "write"):
        logs = sorted((dest / "loadgen" / side).glob("txlog*"))
        sides[side] = p2.side_window(logs, ramp)
    notes = [
        f"read_primary_rate {read_primary_rate}",
        f"read_replica_rate {read_replica_rate}",
        f"write_rate {write_rate}",
        "clients 50+50+50",
        "jobs 8+8+8",
    ]
    invalid = False
    empty = {"scored_lat": [], "scored_exec": [], "ramp_lat": [], "ramp_exec": [], "scored_fail": 0, "ramp_fail": 0, "n_ok": 0}
    for side, window in list(sides.items()):
        if window is None:
            invalid = True
            notes.append(f"missing {side} txlog")
            sides[side] = empty
    lat = sorted(sum((sides[side]["scored_lat"] for side in sides), []))
    execs = sorted(sum((sides[side]["scored_exec"] for side in sides), []))
    ramp_lat = sorted(sum((sides[side]["ramp_lat"] for side in sides), []))
    ramp_exec = sorted(sum((sides[side]["ramp_exec"] for side in sides), []))
    n_ok = sum(sides[side]["n_ok"] for side in sides)
    n_fail = sum(sides[side]["scored_fail"] for side in sides)
    read_ok = sides["read-primary"]["n_ok"] + sides["read-replica"]["n_ok"]
    total_qps = n_ok / seconds if seconds else None
    read_qps = read_ok / seconds if seconds else None
    write_qps = sides["write"]["n_ok"] / seconds if seconds else None
    error_rate = n_fail / (n_ok + n_fail) if (n_ok + n_fail) else None
    p50 = rl.percentile(lat, 50)
    p95 = rl.percentile(lat, 95)
    p99 = rl.percentile(lat, 99)
    exec_p99 = rl.percentile(execs, 99)
    primary_vm = rl.avg_vmstat(next((dest / "db").rglob("vmstat.txt")).read_text(errors="replace"))
    replica_vm_path = next((dest / "replica").rglob("vmstat.txt"), None)
    replica_vm = rl.avg_vmstat(replica_vm_path.read_text(errors="replace")) if replica_vm_path else None
    mp_text = next((dest / "loadgen").rglob("mpstat.txt")).read_text(errors="replace")
    pid_path = next((dest / "loadgen").rglob("pidstat.txt"), None)
    pid_text = pid_path.read_text(errors="replace") if pid_path else ""
    pressure = rl.loadgen_pressure(mp_text, pid_text)
    io_path = next((dest / "db").rglob("iostat.txt"), None)
    io = rl.avg_iostat(io_path.read_text(errors="replace")) if io_path else None
    replica_io_path = next((dest / "replica").rglob("iostat.txt"), None)
    replica_io = rl.avg_iostat(replica_io_path.read_text(errors="replace")) if replica_io_path else None
    lag_path = next((dest / "db").rglob("lag.csv"), None)
    lag = p2.lag_stats(lag_path.read_text(errors="replace")) if lag_path else None
    primary_hit = p2.hit_delta((dest / "before_primary.txt").read_text(), (dest / "after_primary.txt").read_text())
    replica_hit = p2.hit_delta((dest / "before_replica.txt").read_text(), (dest / "after_replica.txt").read_text())
    if primary_vm is None or replica_vm is None:
        invalid = True
        notes.append("missing vmstat")
    for label, vm in (("primary", primary_vm), ("replica", replica_vm)):
        if vm and vm["steal"] > 5:
            invalid = True
            notes.append(f"{label} steal {vm['steal']:.2f}% > 5")
    if pressure["machine_avg"] is None or pressure["core_max"] is None:
        invalid = True
        notes.append("missing per-core mpstat")
    elif pressure["core_max"] > 70:
        invalid = True
        notes.append(f"loadgen core {pressure['core_max']:.2f}% > 70")
    if not pressure["thread_avg"]:
        invalid = True
        notes.append("missing pgbench thread cpu")
    elif pressure["thread_max"] > 70:
        invalid = True
        notes.append(f"pgbench thread {pressure['thread_max']:.2f}% > 70")
    if lag is None:
        invalid = True
        notes.append("missing replication lag")
    else:
        notes.append(
            f"replay_lag_median_s {lag['replay_median']:.3f} replay_lag_max_s {lag['replay_max']:.3f} "
            f"write_lag_max_s {lag['write_max']:.3f} flush_lag_max_s {lag['flush_max']:.3f} lag_n {lag['n']}"
        )
    if pressure["core_max"] is not None:
        notes.append(f"loadgen_core_max {pressure['core_max']:.2f}")
    if pressure["thread_max"] is not None:
        notes.append(f"pgbench_thread_max {pressure['thread_max']:.2f}")
    if exec_p99 is not None:
        notes.append(f"exec_p99 {exec_p99:.3f}")
    if ramp and ramp_lat:
        notes.append(
            f"ramp_{ramp}s_p99 {rl.none_num(rl.percentile(ramp_lat, 99))} "
            f"ramp_exec_p99 {rl.none_num(rl.percentile(ramp_exec, 99))}"
        )
    if io:
        notes.append(f"primary_disk_util {io['util']:.2f}")
    if replica_io:
        notes.append(f"replica_disk_util {replica_io['util']:.2f}")
    if primary_hit is not None:
        notes.append(f"primary_hit {primary_hit:.6f}")
    if replica_hit is not None:
        notes.append(f"replica_hit {replica_hit:.6f}")
    if primary_vm:
        notes.append(f"primary_cpu {primary_vm['cpu']:.2f} primary_iowait {primary_vm['iowait']:.2f}")
    if replica_vm:
        notes.append(f"replica_cpu {replica_vm['cpu']:.2f} replica_iowait {replica_vm['iowait']:.2f}")
    steal = None
    if primary_vm and replica_vm:
        steal = max(primary_vm["steal"], replica_vm["steal"])
    passed = (
        not invalid
        and p95 is not None
        and p99 is not None
        and error_rate is not None
        and p95 < 100
        and p99 < 500
        and error_rate < 0.01
    )
    label = "invalid" if invalid else "pass" if passed else "fail"
    row = {
        "phase": "phase3",
        "scale": "100M",
        "workload": "mixed-8020-split",
        "run_type": run_type,
        "target_rate": "" if offered is None else offered,
        "total_qps": rl.none_num(total_qps),
        "read_qps": rl.none_num(read_qps),
        "write_qps": rl.none_num(write_qps),
        "p50": rl.none_num(p50),
        "p95": rl.none_num(p95),
        "p99": rl.none_num(p99),
        "error_rate": rl.none_num(error_rate, 6),
        "pass_fail": label,
        "avg_cpu": rl.none_num(primary_vm["cpu"] if primary_vm else None),
        "avg_iowait": rl.none_num(primary_vm["iowait"] if primary_vm else None),
        "avg_steal": rl.none_num(steal),
        "cache_hit_ratio": rl.none_num(replica_hit, 6),
        "invalid": "yes" if invalid else "no",
        "run_id": run_id,
        "loadgen_cpu": rl.none_num(pressure["machine_avg"]),
        "notes": "; ".join(notes),
    }
    rl.append_csv(row)
    result = {
        "row": row,
        "passed": passed,
        "invalid": invalid,
        "rate": offered,
        "total_qps": total_qps,
        "read_qps": read_qps,
        "write_qps": write_qps,
        "p95": p95,
        "p99": p99,
        "error_rate": error_rate,
        "primary_hit": primary_hit,
        "hit_ratio": replica_hit,
        "lag": lag,
        "exec_p99": exec_p99,
    }
    (dest / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    rl.progress(
        f"done {run_id} {label} qps={rl.none_num(total_qps)} p95={rl.none_num(p95)} "
        f"p99={rl.none_num(p99)} err={rl.none_num(error_rate, 6)} "
        f"replay_max={'n/a' if lag is None else format(lag['replay_max'], '.3f')}"
    )
    return result


def measure_triple_rerun(run_id, run_type, seconds, offered, replica_host, replica_private, attempts=3):
    last = None
    for attempt in range(1, attempts + 1):
        suffix = "" if attempt == 1 else f"-rerun{attempt}"
        last = measure_triple(f"{run_id}{suffix}", run_type, seconds, offered, replica_host, replica_private)
        if not last["invalid"]:
            return last
        rl.progress(f"{run_id} invalid, attempt {attempt}")
    return last


def split_warmup(replica_host, replica_private):
    run_id = "100m-split-warmup"
    dest = rl.RAW / run_id
    if (dest / "warmup.json").exists():
        rl.progress(f"reusing warmup {run_id}")
        return json.loads((dest / "warmup.json").read_text())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    rl.progress(f"adaptive warmup {run_id} min {rl.WARMUP_MIN_S}s max {rl.WARMUP_MAX_S}s")
    jobs = {
        "read-primary": p2.pgbench_script(run_id, "read-primary", rl.DB_PRIVATE, p2.READ_FILES, rl.WARMUP_MAX_S, None, progress=60),
        "read-replica": p2.pgbench_script(run_id, "read-replica", replica_private, p2.READ_FILES, rl.WARMUP_MAX_S, None, progress=60),
        "write": p2.pgbench_script(run_id, "write", rl.DB_PRIVATE, p2.WRITE_FILES, rl.WARMUP_MAX_S, None, progress=60),
    }
    names = []
    for side, script in jobs.items():
        name = f"p3-{run_id}-{side}"
        names.append(name)
        p2.begin_job(rl.LOAD, name, script)
    rl.start_collectors(run_id, all_pgbench=True)
    p2.start_replica_collectors(run_id, replica_host)
    windows = []
    prev = {"primary": None, "replica": None}
    stable = False
    capped = False
    started = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started
            p2.guard(run_id)
            if elapsed > rl.WARMUP_MAX_S + 120:
                capped = True
                break
            time.sleep(20)
            nows = []
            missing = False
            for side in jobs:
                text = rl.ssh(
                    rl.LOAD,
                    f"cat /var/tmp/pg-limit/{run_id}/{side}/summary.txt 2>/dev/null || true",
                    check=False,
                ).stdout
                sample = p2.latest_tps(text)
                if sample is None:
                    missing = True
                else:
                    nows.append(sample)
            if missing:
                if elapsed > 90:
                    alive = rl.ssh(
                        rl.LOAD,
                        "for s in " + " ".join(names) + "; do "
                        "if tmux has-session -t $s 2>/dev/null; then echo $s UP; else echo $s DOWN; fi; done",
                        check=False,
                    ).stdout
                    if "DOWN" in alive:
                        raise RuntimeError(f"warmup pgbench exited early\n{alive}")
                continue
            mark_s = min(sample[0] for sample in nows)
            if windows and mark_s <= windows[-1]["elapsed_s"]:
                continue
            hits = {}
            for label, host in (("primary", rl.DB), ("replica", replica_host)):
                stats = p2.psql_on(
                    host,
                    "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
                ).strip()
                hit, read = [int(part) for part in stats.split(",")]
                interval = None
                if prev[label] is not None:
                    d_hit, d_read = hit - prev[label][0], read - prev[label][1]
                    if d_hit + d_read:
                        interval = d_hit / (d_hit + d_read)
                prev[label] = (hit, read)
                hits[label] = interval
            qps = sum(sample[1] for sample in nows)
            windows.append({"elapsed_s": mark_s, "qps": qps, "primary_hit": hits["primary"], "replica_hit": hits["replica"]})
            rl.progress(
                f"warmup {run_id} t={mark_s:.0f}s qps={qps:.1f} primary_hit={hits['primary']} replica_hit={hits['replica']}"
            )
            if mark_s >= rl.WARMUP_MIN_S and len(windows) >= 3:
                tail = windows[-3:]
                if all(window["primary_hit"] is not None and window["replica_hit"] is not None for window in tail):
                    qvals = [window["qps"] for window in tail]
                    qmean = sum(qvals) / 3
                    qspan = (max(qvals) - min(qvals)) / qmean if qmean else 1
                    hspan = max(
                        max(window["primary_hit"] for window in tail) - min(window["primary_hit"] for window in tail),
                        max(window["replica_hit"] for window in tail) - min(window["replica_hit"] for window in tail),
                    )
                    if qspan <= rl.WARMUP_QPS_SPAN and hspan <= rl.WARMUP_HIT_SPAN:
                        stable = True
                        break
            if mark_s >= rl.WARMUP_MAX_S:
                capped = True
                break
    finally:
        p2.kill_jobs(rl.LOAD, names)
        rl.stop_collectors(run_id)
        p2.stop_replica_collectors(run_id, replica_host)
    info = {"mode": "adaptive", "elapsed_s": time.monotonic() - started, "stable": stable, "capped": capped, "windows": windows}
    (dest / "warmup.json").write_text(json.dumps(info, indent=2) + "\n")
    rl.progress(f"warmup done {run_id} seconds={info['elapsed_s']:.0f} stable={stable} capped={capped}")
    return info


def run_split(replica_host, replica_private):
    prepare_bench()
    wait_lag_under(1.0)
    warmup = split_warmup(replica_host, replica_private)
    wait_lag_under(1.0)
    ceiling = measure_triple_rerun("100m-split-ceiling", "ceiling", 120, None, replica_host, replica_private, attempts=2)
    summary = {
        "prefix": "100m-split",
        "scale": "100M",
        "workload": "mixed-8020-split",
        "read_primary_share": 0.45,
        "read_replica_share": 0.45,
        "write_share": 0.10,
        "warmup": warmup,
        "ceiling_qps": ceiling["total_qps"],
        "ceiling": ceiling["row"],
        "confirmed_rate": None,
        "attempts": [],
        "search_failures": [],
        "stopped_reason": None,
    }
    if not ceiling["total_qps"]:
        summary["stopped_reason"] = "ceiling produced no qps"
        return summary
    lo = 0
    hi = max(1, int(math.floor(ceiling["total_qps"])))
    best = None
    step = 0
    while hi - lo > max(1, 0.05 * hi):
        mid = (lo + hi) // 2
        if mid <= lo or mid >= hi:
            break
        step += 1
        wait_lag_under(1.0)
        result = measure_triple_rerun(f"100m-split-search-{step:02d}", "search", 120, mid, replica_host, replica_private)
        if result["invalid"] or not result["passed"]:
            summary["search_failures"].append(result["row"])
            hi = mid
        else:
            best = result
            lo = mid
    if best is None:
        summary["stopped_reason"] = "no 2-minute search rate passed"
        return summary
    current = int(best["rate"])
    stepdowns = 0
    while current >= 1:
        results = []
        failed = False
        for i in (1, 2, 3):
            wait_lag_under(1.0)
            result = measure_triple_rerun(
                f"100m-split-confirm-r{current}-{i}", "confirm", 300, current, replica_host, replica_private
            )
            results.append(result)
            if result["invalid"] or not result["passed"]:
                failed = True
                break
        ok = not failed and len(results) == 3 and all(item["passed"] and not item["invalid"] for item in results)
        summary["attempts"].append(
            {
                "rate": current,
                "passed": ok,
                "rows": [item["row"] for item in results],
                "lags": [item["lag"] for item in results],
                "primary_hits": [item["primary_hit"] for item in results],
            }
        )
        if ok:
            summary["confirmed_rate"] = current
            break
        if stepdowns >= 5:
            summary["stopped_reason"] = "5-minute confirms failed and step-down limit of 5 was reached"
            break
        nxt = int(math.floor(current * 0.95))
        if nxt >= current:
            nxt = current - 1
        rl.progress(f"split confirm at {current} failed; stepping down to {nxt}")
        current = nxt
        stepdowns += 1
    summary["end_db_bytes"], summary["end_wal_bytes"] = rl.wal_and_db_bytes()
    return summary


def build_replica():
    if "split-streaming" in done_set():
        vals = p2.state_values()
        return vals["REPLICA_PUBLIC"], vals["REPLICA_PRIVATE"]
    droplet_id, public, private = create_droplet("pg-limit-replica", SMALL_SIZE)
    hourly = SMALL_HOURLY
    chosen = p2.size_price(SMALL_SIZE)
    if chosen is not None:
        hourly = float(chosen["price_hourly"])
    p2.update_state(
        {
            "REPLICA_ID": droplet_id,
            "REPLICA_NAME": "pg-limit-replica",
            "REPLICA_PUBLIC": public,
            "REPLICA_PRIVATE": private,
            "REPLICA_SIZE": SMALL_SIZE,
            "REPLICA_HOURLY": str(hourly),
            "REPLICA_DESTROYED": "no",
        }
    )
    p2.allow_postgres_from(private)
    p2.add_billing(SMALL_HOURLY + hourly + LOAD_HOURLY, "primary c5-4 plus replica c5-4 plus load generator")
    p2.install_replica_packages(public)
    if not p2.streaming(rl.DB, public):
        p2.ensure_replicator(rl.DB)
        p2.basebackup(p2.state_values()["DB_PRIVATE"], public)
    p2.point_loadgen_at_replica(private)
    p2.ensure_replica_hba(public)
    p2.wait_streaming(rl.DB, public)
    wait_lag_under(1.0)
    mark("split-streaming")
    rl.progress(f"replica streaming with replay lag under 1s at {public} {private}")
    return public, private


def row_from_summary(summary):
    if summary.get("replacement"):
        summary = summary["replacement"]
    rows = p2.passing_rows(summary)
    if not rows and summary.get("attempts"):
        for attempt in summary["attempts"]:
            if attempt.get("passed"):
                rows = attempt.get("rows") or []
    return rows


def fmt_row(label, summary, ram, broke):
    rows = row_from_summary(summary)
    db_bytes = summary.get("end_db_bytes")
    if summary.get("replacement"):
        db_bytes = summary["replacement"].get("end_db_bytes")
    return (
        "| {label} | {offered} | {measured} | {read} | {write} | {users} | {p50} | {p95} | {p99} | {size} | {hit} | {broke} |"
    ).format(
        label=label,
        offered="" if not summary.get("confirmed_rate") else summary["confirmed_rate"],
        measured=p2.fmt_triple(rows, "total_qps"),
        read=p2.fmt_triple(rows, "read_qps"),
        write=p2.fmt_triple(rows, "write_qps"),
        users=p2.users_of(rows),
        p50=p2.fmt_triple(rows, "p50"),
        p95=p2.fmt_triple(rows, "p95"),
        p99=p2.fmt_triple(rows, "p99"),
        size=p2.size_cell(db_bytes, ram),
        hit=p2.fmt_triple(rows, "cache_hit_ratio", 6),
        broke=broke.replace("|", "/"),
    )


def gib(n):
    return f"{n / GIB:.2f} GiB"


def explanation():
    hot = json.loads((ROOT / "results" / "hot-set.json").read_text()) if (ROOT / "results" / "hot-set.json").exists() else None
    replica = json.loads((ROOT / "results" / "summary-100m-replica.json").read_text())
    big = json.loads((ROOT / "results" / "summary-100m-16gb.json").read_text())
    mixed = json.loads((ROOT / "results" / "summary-100m-v2.json").read_text())
    ceiling = replica.get("ceiling") or {}
    fail = next((row for row in replica.get("search_failures") or [] if str(row.get("run_id", "")).endswith("search-01")), None)
    lines = [
        "## Write-only confirms and the replica",
        "",
        "The three write-only confirms at offered 1,522 ran with no replica attached and no base backup in progress. "
        "pgbench for confirm 1 started at 2026-10-06T15:38:33Z and the scored run was done at 2026-10-06T15:46:56Z. "
        "Confirm 2 started at 2026-10-06T15:49:15Z and was done at 2026-10-06T15:57:41Z. "
        "Confirm 3 started at 2026-10-06T16:00:00Z and was done at 2026-10-06T16:08:32Z. "
        "The summary was written at 2026-10-06T16:10:53Z. "
        "The replica droplet was created at 2026-10-06T16:11:15Z and existed at 2026-10-06T16:11:57Z. "
        "pg_basebackup started at 2026-10-06T16:14:03Z and finished at 2026-10-06T16:34:30Z. "
        "The write-only limit was not rerun.",
        "",
        "## Why all-reads-on-replica warmed up near 16,500 and confirmed 7,735",
        "",
        "The warmup and the ceiling were closed loop: two pgbench processes, no `--rate`. "
        f"At 600 seconds the warmup's combined progress rate was 17,963 queries per second, and the replica cache hit was about 0.64. "
        f"The ceiling measured {ceiling.get('total_qps')} QPS with logged p95 {ceiling.get('p95')} ms and p99 {ceiling.get('p99')} ms. "
        f"Its notes record replay lag max {((ceiling.get('notes') or '').split('replay_lag_max_s ')[1].split()[0]) if 'replay_lag_max_s' in (ceiling.get('notes') or '') else 'n/a'} seconds. "
        "Closed loop has no schedule lag. Each side runs as fast as 50 clients allow, so the write side was about 3,500 QPS rather than 10% of the total, and logged time is the query time. "
        "The replica kept serving those reads while it fell minutes behind on WAL.",
        "",
    ]
    if fail:
        lines.append(
            f"The first open-loop search, offered {fail.get('target_rate')}, failed with logged p95 {fail.get('p95')} ms and p99 {fail.get('p99')} ms. "
            f"Execution p99 in that run's notes is inside `{fail.get('notes')}`. "
            "Logged time already includes schedule lag, so a rate the clients cannot start on time fails even when the query itself is still tens of milliseconds. "
            "That search started after the ceiling, which had left replay lag at about 184 seconds, and the search's own replay lag median stayed above 100 seconds. "
            "The three confirms at 7,735 then ran with replay lag max 0.274 s, 0.101 s, and 0.026 s and logged p95 near 4 ms. "
            "7,735 is the rate that passed while the replica was caught up. The closed-loop 16,500 figure is a different mix and a different latency rule."
        )
        lines.append("")
    lines.extend(
        [
            "## Why 8 vCPU / 16 GB was 3.6×",
            "",
            f"30,417 / 8,505 = {30417 / 8505:.2f}. "
            f"On the 8 GB box the published 100M mixed confirms had cache hit {p2.fmt_triple(p2.passing_rows(mixed), 'cache_hit_ratio', 3)} "
            f"and the next rate, offered 8,859, failed logged p95 at 128.708 ms while execution p99 stayed near 33 ms. "
            f"On 16 GB the three confirms at 30,417 had cache hit {p2.fmt_triple(p2.passing_rows(big), 'cache_hit_ratio', 3)}, "
            "CPU about 82–86%, iowait about 5.5–7.3%, and scored execution p99 about 5.5–5.8 ms. "
            "Offered 31,466 then failed logged p99 at 508.417 ms with scored execution p99 5.158 ms. "
            "The 16 GB box ran out of schedule, at about 85% CPU. The 8 GB box ran out of logged p95 while queries were still waiting on disk.",
            "",
        ]
    )
    if hot:
        rel = hot["relations"]
        lines.append(
            "The hot 20% is the newest fifth of the loaded ids, which is where 80% of the draws land: "
            f"posts {HOT_POSTS:,} of {hot['counts']['posts']:,} ({hot['fractions']['posts']:.3f} of the posts heap and indexes), "
            f"users {HOT_USERS:,} of {hot['counts']['users']:,} ({hot['fractions']['users']:.3f}), "
            f"and likes on those posts. Loaded likes were {LOADED_LIKES:,}. "
            f"Likes above that are {max(0, hot['counts']['likes'] - LOADED_LIKES):,}, and 80% of like writes hit the hot posts, "
            f"so the hot like estimate is {hot['fractions']['hot_likes']:.0f} rows, {hot['fractions']['likes']:.3f} of the likes heap and indexes."
        )
        lines.append("")
        lines.append(
            f"Measured sizes: posts heap {gib(rel['posts']['heap'])} plus indexes {gib(rel['posts']['indexes'])}, "
            f"users heap {gib(rel['users']['heap'])} plus indexes {gib(rel['users']['indexes'])}, "
            f"likes heap {gib(rel['likes']['heap'])} plus indexes {gib(rel['likes']['indexes'])}. "
            f"The hot slice is {gib(hot['hot_bytes']['posts'])} of posts, {gib(hot['hot_bytes']['users'])} of users, "
            f"and {gib(hot['hot_bytes']['likes'])} of likes, {gib(hot['hot_total'])} together."
        )
        lines.append("")
        lines.append(
            f"OS page cache after `shared_buffers` is about {gib(hot['page_cache_8'])} on the 8 GB box (8 GiB minus 2 GiB) "
            f"and about {gib(hot['page_cache_16'])} on the 16 GB box (16 GiB minus 4 GiB). "
            + (
                "It fits in 8 GiB of RAM. "
                if hot["hot_total"] <= 8 * GIB
                else "It does not fit in 8 GiB of RAM. "
            )
            + (
                "It also fits in the page cache left on that box. "
                if hot["hot_total"] <= hot["page_cache_8"]
                else "It is larger than the page cache left on that box. "
            )
            + (
                "On the 16 GB machine it fits in 16 GiB of RAM"
                if hot["hot_total"] <= 16 * GIB
                else "On the 16 GB machine it does not fit in 16 GiB of RAM"
            )
            + (
                " and in the page cache left after `shared_buffers`. "
                if hot["hot_total"] <= hot["page_cache_16"]
                else " and is just over the page cache left after `shared_buffers`. "
            )
            + "`shared_buffers` itself is 2 GiB and 4 GiB, so the full hot slice was never inside `shared_buffers` on either box. "
            "The whole database stayed near 63 GiB, several times either machine."
        )
        lines.append("")
    return lines


def patch_results():
    path = ROOT / "results" / "RESULTS.md"
    text = path.read_text()
    text = text.replace("| 100M replica |", "| 100M all reads on replica |", 1)
    recheck_path = ROOT / "results" / "summary-100m-8gb-recheck.json"
    split_path = ROOT / "results" / "summary-100m-split.json"
    extra = []
    if recheck_path.exists():
        recheck = json.loads(recheck_path.read_text())
        if recheck.get("stood"):
            broke = "Three 5-minute confirms at 8,505 passed after the resize back to 8 GB. 8,505 stands."
            label = "100M mixed recheck"
            offered_summary = recheck
        else:
            replacement = recheck.get("replacement") or {}
            broke = replacement.get("stopped_reason") or "8,505 did not pass three confirms. This row is the new single-box limit."
            fail = p2.nearest_failure(replacement) if replacement else None
            if fail:
                broke = (
                    f"8,505 did not pass. Offered {fail.get('target_rate')} failed with p95 {fail.get('p95')} ms "
                    f"and p99 {fail.get('p99')} ms (run {fail.get('run_id')})."
                )
            label = "100M mixed recheck"
            offered_summary = recheck
        line = fmt_row(label, offered_summary, 8 * GIB, broke)
        if line not in text and "100M mixed recheck" not in text:
            extra.append(line)
    if split_path.exists():
        split = json.loads(split_path.read_text())
        fail = p2.nearest_failure(split)
        if fail:
            broke = (
                f"Offered {fail.get('target_rate')} failed with p95 {fail.get('p95')} ms and p99 {fail.get('p99')} ms "
                f"(run {fail.get('run_id')}). Reads were split 45/45 across primary and replica."
            )
        elif split.get("confirmed_rate"):
            lags = []
            for attempt in split.get("attempts") or []:
                if attempt.get("passed"):
                    lags = attempt.get("lags") or []
            lag_txt = ", ".join(
                "n/a" if item is None else f"{item.get('replay_max'):.3f}s" for item in lags
            )
            broke = f"Confirmed with reads split evenly. Confirm replay lag max: {lag_txt}."
        else:
            broke = split.get("stopped_reason") or "not confirmed"
        line = fmt_row("100M split reads", split, 8 * GIB, broke)
        if "100M split reads" not in text:
            extra.append(line)
    if extra:
        needle = "| 100M 8 vCPU / 16 GB |"
        at = text.find(needle)
        end = text.find("\n", at)
        text = text[: end + 1] + "\n".join(extra) + "\n" + text[end + 1 :]
    block = ["", *explanation()]
    marker = "## This round"
    if marker not in text:
        insert_at = text.find("## Droplets, time, and cost")
        if insert_at == -1:
            insert_at = text.find("## Method history")
        text = text[:insert_at] + "\n".join(block) + "\n" + text[insert_at:]
    events = json.loads((ROOT / "results" / "billing-events.json").read_text())
    spent = rl.spend_usd()
    from datetime import datetime, timezone

    started = datetime(2026, 10, 5, 12, 41, 29, tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    cost_note = (
        f"\nUpdated cost after this round, {p2.stamp()}: {hours:.2f} hours from 2026-10-05T12:41:29Z, ${spent:.2f} total. "
        "The primary is c5-4vcpu-8gb again at $0.18452/hr. The primary, the load generator, and the replica stay up for the case study.\n"
    )
    if "Updated cost after this round" not in text:
        text = text.replace("## Method history", cost_note + "\n## Method history", 1)
    else:
        start = text.find("Updated cost after this round")
        end = text.find("\n## Method history", start)
        text = text[:start] + cost_note.strip() + "\n" + text[end + 1 :]
    rate_header = "Rate changes:\n"
    if rate_header in text:
        pass
    # Append any billing events that are not already listed.
    for event in events:
        bullet = f"- {event['at']}: ${event['hourly']:.5f}/hr, {event.get('note', '')}"
        if bullet not in text:
            text = text.replace("## Method history", bullet + "\n\n## Method history", 1)
    path.write_text(text)


def destroy_named(droplet_id, expected_name, flag):
    vals = p2.state_values()
    if not droplet_id or vals.get(flag) == "yes":
        return
    try:
        detail = p2.droplet_detail(droplet_id)
    except RuntimeError:
        p2.update_state({flag: "yes"})
        return
    if detail.get("name") != expected_name or "pg-limit-test" not in (detail.get("tags") or []):
        raise p2.StopAndAsk(f"refusing to destroy {droplet_id}; name={detail.get('name')} tags={detail.get('tags')}")
    if str(droplet_id) == DB_ID:
        raise p2.StopAndAsk("refusing to destroy the primary")
    rl.doctl(["compute", "droplet", "delete", str(droplet_id), "--force"])
    p2.update_state({flag: "yes"})
    rl.progress(f"destroyed {expected_name} {droplet_id}")


def safe_destroy():
    vals = p2.state_values()
    replica_id = vals.get("REPLICA_ID")
    if replica_id and vals.get("REPLICA_DESTROYED") != "yes":
        host = vals.get("REPLICA_PUBLIC", "")
        if host:
            rl.ssh(host, "systemctl stop postgresql@18-main || true", check=False)
        for _ in range(12):
            active = p2.psql_on(
                rl.DB,
                "SELECT count(*) FROM pg_replication_slots WHERE active;",
                database="postgres",
            ).strip()
            if active == "0":
                break
            time.sleep(5)
        drop_idle_slots()
        destroy_named(replica_id, "pg-limit-replica", "REPLICA_DESTROYED")
        p2.add_billing(SMALL_HOURLY + LOAD_HOURLY, "primary c5-4 plus load generator, replica destroyed")
    vals = p2.state_values()
    if vals.get("LOAD_ID") and vals.get("LOAD_DESTROYED") != "yes":
        destroy_named(vals["LOAD_ID"], "pg-limit-loadgen", "LOAD_DESTROYED")
        p2.add_billing(SMALL_HOURLY, "primary c5-4vcpu-8gb only; load generator destroyed")


def run():
    rl.progress(
        "write-only confirms at 1522 ended 2026-10-06T16:08:32Z; "
        "replica create started 2026-10-06T16:11:15Z; basebackup 16:14:03Z-16:34:30Z; not rerunning write-only"
    )
    resize_down()
    recreate_loadgen()
    sanity()
    if "split-limit" not in done_set():
        replica_public, replica_private = build_replica()
        summary = run_split(replica_public, replica_private)
        (ROOT / "results" / "summary-100m-split.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
        rl.progress(f"100m-split confirmed={summary.get('confirmed_rate')} reason={summary.get('stopped_reason')}")
        mark("split-limit")


def main():
    if other_phase3():
        raise SystemExit("phase3 is already running")
    try:
        run()
    except p2.StopAndAsk as exc:
        rl.progress(f"STOP: {exc}")
        (ROOT / "results" / "phase3-blocked.txt").write_text(str(exc) + "\n")
        try:
            patch_results()
        except Exception as patch_exc:
            rl.progress(f"results patch failed: {patch_exc}")
        print(f"STOP: {exc}", flush=True)
        return
    except RuntimeError as exc:
        if "pass $60" in str(exc):
            rl.progress(f"STOP: {exc}")
            print(f"STOP: {exc}", flush=True)
            return
        raise
    patch_results()
    rl.progress("phase 3 finished; primary, load generator, and replica kept")
    print("PHASE3 DONE", flush=True)


if __name__ == "__main__":
    main()
