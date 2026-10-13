#!/usr/bin/env python3
"""Phase 2: a read replica, then a bigger primary. Starts only after write-only confirms.

Reads (profile, open post) go to the replica. Writes (like, new post) go to the
primary. The offered rate stays 90% reads and 10% writes. After that limit is
confirmed, the replica is stopped and its replication slot is dropped, the
primary is resized to 8 vCPU and 16 GB without touching the disk, and the mixed
limit is measured again on that primary alone.
"""

import json
import math
import os
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import run_limit as rl

ROOT = rl.ROOT
REPLICA_NAME = "pg-limit-replica"
REPLICA_SIZE = "c5-4vcpu-8gb"
BIGGER_SIZE = "c5-8vcpu-16gb"
DB_ID = "606397622"
LOAD_ID = "606292658"
READ_FILES = (("profile.sql", 50), ("open_post.sql", 50))
WRITE_FILES = (("like.sql", 80), ("new_post.sql", 20))
CKPT = ROOT / "results" / "phase2-checkpoint.txt"
GIB = 1024**3


class StopAndAsk(RuntimeError):
    """A condition the user asked to confirm before continuing."""


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def phase1_running():
    proc = subprocess.run(
        ["pgrep", "-f", "run_limit.py phase1-resume"],
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def other_phase2():
    proc = subprocess.run(
        ["pgrep", "-f", "python3 -u infra/phase2.py"],
        text=True,
        capture_output=True,
    )
    pids = [int(part) for part in proc.stdout.split() if part.isdigit()]
    parent = os.getppid()
    return any(pid not in {os.getpid(), parent} for pid in pids)


def done_set():
    if not CKPT.exists():
        return set()
    return set(CKPT.read_text().split())


def mark(token):
    done = done_set()
    done.add(token)
    CKPT.write_text(" ".join(sorted(done)) + "\n")


def state_values():
    vals = {}
    for line in (ROOT / "infra" / "state.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            vals[key] = value
    return vals


def update_state(changes):
    vals = state_values()
    vals.update(changes)
    path = ROOT / "infra" / "state.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in vals.items()))
    path.chmod(0o600)
    rl.load_state()


def guard(where):
    spent = rl.spend_usd()
    rl.progress(f"spend ${spent:.2f} at {where}")
    if spent >= 58:
        raise StopAndAsk(f"spend ${spent:.2f} would pass $60 before {where}")
    return spent


def init_billing():
    path = ROOT / "results" / "billing-events.json"
    if path.exists():
        return
    switch = json.loads((ROOT / "results" / "billing-switch.json").read_text())
    rows = [
        {
            "at": "2026-10-05T12:41:29Z",
            "hourly": switch["old_hourly"],
            "note": "shared database plus load generator",
        },
        {
            "at": switch["at"],
            "hourly": switch["new_hourly"],
            "note": "dedicated c5-4vcpu-8gb database plus load generator",
        },
    ]
    path.write_text(json.dumps(rows, indent=2) + "\n")


def add_billing(hourly, note):
    path = ROOT / "results" / "billing-events.json"
    rows = json.loads(path.read_text())
    rows.append({"at": stamp(), "hourly": round(hourly, 5), "note": note})
    path.write_text(json.dumps(rows, indent=2) + "\n")


def droplet_detail(droplet_id):
    raw = json.loads(rl.doctl(["compute", "droplet", "get", str(droplet_id), "-o", "json"]))
    return raw[0] if isinstance(raw, list) else raw


def droplet_ips(detail):
    nets = detail["networks"]["v4"]
    public = next(net["ip_address"] for net in nets if net["type"] == "public")
    private = next(net["ip_address"] for net in nets if net["type"] == "private")
    return public, private


def droplet_disk(detail):
    if detail.get("disk") is None:
        return None
    return int(detail["disk"])


def require_write_only():
    if phase1_running():
        raise SystemExit("phase1-resume is still running")
    path = ROOT / "results" / "summary-100m-v2-write.json"
    if not path.exists():
        raise StopAndAsk("the write-only summary was not written")
    summary = json.loads(path.read_text())
    if summary.get("stopped_reason") or not summary.get("confirmed_rate"):
        raise StopAndAsk(
            "the 100M write-only limit did not confirm after the step-down limit: "
            f"confirmed={summary.get('confirmed_rate')} reason={summary.get('stopped_reason')}"
        )
    rl.progress(f"write-only confirmed at {summary['confirmed_rate']}; starting phase 2")
    return summary


def allow_postgres_from(ip):
    if not ip.startswith("10.130."):
        raise StopAndAsk(f"refusing to open 5432 to {ip}")
    want = f"{ip}/32"
    raw = json.loads(rl.doctl(["compute", "firewall", "get", rl.FIREWALL_ID, "-o", "json"]))
    fw = raw[0] if isinstance(raw, list) else raw
    addrs = []
    for rule in fw.get("inbound_rules") or []:
        if rule.get("protocol") == "tcp" and str(rule.get("ports")) == "5432":
            addrs.extend((rule.get("sources") or {}).get("addresses") or [])
    if rl.POSTGRES_SOURCE not in addrs:
        raise StopAndAsk(f"5432 is missing the load generator; current sources {addrs}")
    if want in addrs:
        return
    rl.doctl(
        [
            "compute",
            "firewall",
            "add-rules",
            rl.FIREWALL_ID,
            "--inbound-rules",
            f"protocol:tcp,ports:5432,address:{want}",
        ]
    )
    rl.log_cmd("firewall", f"5432 added {want}; kept {', '.join(addrs)}")
    rl.progress(f"firewall 5432 allows {want}")


def wait_ssh(host):
    for _ in range(60):
        try:
            rl.ssh(host, "echo UP")
            return
        except RuntimeError:
            rl.ensure_ssh_ip()
            time.sleep(10)
    raise StopAndAsk(f"ssh to {host} did not come up")


def psql_on(host, sql, database="social"):
    script = (
        f"sudo -u postgres psql -d {database} -v ON_ERROR_STOP=1 -At <<'SQL'\n{sql}\nSQL"
    )
    return rl.ssh(host, script).stdout


def ensure_replica_droplet():
    vals = state_values()
    if vals.get("REPLICA_ID"):
        detail = droplet_detail(vals["REPLICA_ID"])
        if detail.get("name") != REPLICA_NAME or "pg-limit-test" not in (detail.get("tags") or []):
            raise StopAndAsk(f"replica id {vals['REPLICA_ID']} is not {REPLICA_NAME}")
        public, private = droplet_ips(detail)
        return vals["REPLICA_ID"], public, private
    existing = json.loads(rl.doctl(["compute", "droplet", "list", "-o", "json"]))
    ours = [item for item in existing if "pg-limit-test" in (item.get("tags") or [])]
    found = next((item for item in ours if item.get("name") == REPLICA_NAME), None)
    if found is None:
        if len(ours) >= 3:
            raise StopAndAsk("already at 3 tagged droplets; not creating another")
        guard("create replica")
        created = json.loads(
            rl.doctl(
                [
                    "compute", "droplet", "create", REPLICA_NAME,
                    "--region", "lon1",
                    "--size", REPLICA_SIZE,
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
        raise StopAndAsk("refusing to treat the primary as the replica")
    try:
        rl.doctl(
            [
                "compute", "firewall", "add-droplets", rl.FIREWALL_ID,
                "--droplet-ids", droplet_id,
            ]
        )
    except RuntimeError as exc:
        rl.progress(f"firewall add skipped: {exc}")
    detail = droplet_detail(droplet_id)
    public, private = droplet_ips(detail)
    update_state(
        {
            "REPLICA_ID": droplet_id,
            "REPLICA_NAME": REPLICA_NAME,
            "REPLICA_PUBLIC": public,
            "REPLICA_PRIVATE": private,
            "REPLICA_SIZE": REPLICA_SIZE,
            "REPLICA_HOURLY": "0.18452",
        }
    )
    allow_postgres_from(private)
    wait_ssh(public)
    add_billing(0.18452 + 0.18452 + 0.25, "primary c5-4 plus replica c5-4 plus load generator")
    rl.progress(f"replica {droplet_id} public {public} private {private}")
    return droplet_id, public, private


def ensure_repl_password():
    path = ROOT / "infra" / "repl.pass"
    if not path.exists():
        path.write_text(os.urandom(18).hex() + "\n")
        path.chmod(0o600)
    return path


def install_replica_packages(host):
    ready = rl.ssh(
        host,
        "test -x /usr/lib/postgresql/18/bin/pg_basebackup && echo YES || echo NO",
    ).stdout.strip()
    if ready == "YES":
        rl.progress("replica packages already installed")
        return
    script = r"""#!/bin/bash
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
apt-get install -y postgresql-common ca-certificates curl gnupg sysstat tmux
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt-get install -y postgresql-18 postgresql-contrib-18
systemctl stop postgresql
echo "STEP|packages"
"""
    rl.remote_job(host, script, "replica-packages", timeout_s=3600, restart=True)
    rl.progress("replica packages installed and postgres stopped")


def ensure_replicator(primary_host):
    rl.ssh(primary_host, "mkdir -p /root/pg-limit && chmod 700 /root/pg-limit")
    rl.scp_to(primary_host, ensure_repl_password(), "/root/pg-limit/repl.pass")
    rl.ssh(primary_host, "chmod 600 /root/pg-limit/repl.pass")
    shown = psql_on(primary_host, "SHOW wal_level;", database="postgres").strip()
    if shown != "replica":
        raise StopAndAsk(f"primary wal_level is {shown}; not changing it automatically")
    hba = rl.ssh(
        primary_host,
        "grep -c 'replicator' /etc/postgresql/18/main/pg_hba.conf || true",
    ).stdout.strip()
    if hba == "0":
        rl.ssh(
            primary_host,
            "printf '%s\\n' 'host replication replicator 10.130.0.0/20 scram-sha-256' >> /etc/postgresql/18/main/pg_hba.conf",
        )
        psql_on(primary_host, "SELECT pg_reload_conf();", database="postgres")
    rl.ssh(
        primary_host,
        """
sudo -u postgres psql -d postgres -q -v ON_ERROR_STOP=1 -v pass="$(cat /root/pg-limit/repl.pass)" > /root/pg-limit/repl-role.log 2>&1 <<'SQL'
SELECT format('CREATE ROLE replicator LOGIN REPLICATION PASSWORD %L', :'pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') \\gexec
SELECT format('ALTER ROLE replicator WITH LOGIN REPLICATION PASSWORD %L', :'pass') \\gexec
SQL
chmod 600 /root/pg-limit/repl-role.log
echo role_ok
""",
    )


def streaming(primary_host, replica_host):
    try:
        recovery = psql_on(replica_host, "SELECT pg_is_in_recovery();", database="postgres").strip()
    except RuntimeError:
        return False
    if recovery != "t":
        return False
    count = psql_on(
        primary_host,
        "SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming';",
        database="postgres",
    ).strip()
    return count == "1"


def basebackup(primary_private, replica_host):
    rl.ssh(replica_host, "mkdir -p /root/pg-limit && chmod 700 /root/pg-limit")
    rl.scp_to(replica_host, ROOT / "infra" / "repl.pass", "/root/pg-limit/repl.pass")
    rl.ssh(replica_host, "chmod 600 /root/pg-limit/repl.pass")
    rl.ssh(
        replica_host,
        f"""
python3 - <<'PY'
from pathlib import Path
pw = Path("/root/pg-limit/repl.pass").read_text().strip()
path = Path("/var/lib/postgresql/.pgpass")
path.write_text("{primary_private}:5432:*:replicator:" + pw + "\\n")
path.chmod(0o600)
PY
chown postgres:postgres /var/lib/postgresql/.pgpass
""",
    )
    inactive = psql_on(
        rl.DB,
        "SELECT slot_name FROM pg_replication_slots WHERE slot_name = 'pg_limit_replica' AND NOT active;",
        database="postgres",
    ).strip()
    if inactive == "pg_limit_replica":
        psql_on(
            rl.DB,
            "SELECT pg_drop_replication_slot('pg_limit_replica');",
            database="postgres",
        )
    script = f"""
set -euo pipefail
systemctl stop postgresql@18-main || true
systemctl stop postgresql || true
rm -rf /var/lib/postgresql/18/main
install -d -o postgres -g postgres -m 700 /var/lib/postgresql/18/main
sudo -u postgres env PGPASSFILE=/var/lib/postgresql/.pgpass \\
  pg_basebackup -h {primary_private} -p 5432 -U replicator \\
  -D /var/lib/postgresql/18/main -Fp -Xs -P -R -C -S pg_limit_replica
echo "STEP|basebackup-done"
"""
    rl.remote_job(replica_host, script, "replica-basebackup", timeout_s=4 * 3600, restart=True)
    start_replica_postgres(replica_host)


def start_replica_postgres(replica_host):
    private = state_values()["REPLICA_PRIVATE"]
    conf = (ROOT / "results" / "postgres-dedicated.conf").read_text()
    lines = []
    for line in conf.splitlines():
        if line.startswith("listen_addresses"):
            lines.append(f"listen_addresses = '{private}'")
        else:
            lines.append(line)
    if not any(line.startswith("hot_standby_feedback") for line in lines):
        lines.append("hot_standby_feedback = on")
    local = ROOT / "results" / "postgres-replica.conf"
    local.write_text("\n".join(lines) + "\n")
    rl.ssh(replica_host, "mkdir -p /etc/postgresql/18/main/conf.d")
    rl.scp_to(replica_host, local, "/etc/postgresql/18/main/conf.d/pg-limit.conf")
    rl.ssh(
        replica_host,
        """
set -e
chown postgres:postgres /etc/postgresql/18/main/conf.d/pg-limit.conf
chmod 644 /etc/postgresql/18/main/conf.d/pg-limit.conf
systemctl reset-failed postgresql@18-main || true
systemctl start postgresql@18-main
systemctl is-active postgresql@18-main
""",
    )


def ensure_replica_hba(replica_host):
    rl.scp_to(replica_host, ROOT / "infra" / "pg_hba.conf", "/etc/postgresql/18/main/pg_hba.conf")
    rl.ssh(
        replica_host,
        """
set -e
chown postgres:postgres /etc/postgresql/18/main/pg_hba.conf
chmod 640 /etc/postgresql/18/main/pg_hba.conf
sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 -c 'SELECT pg_reload_conf();'
""",
    )


def wait_streaming(primary_host, replica_host):
    for _ in range(60):
        if streaming(primary_host, replica_host):
            lag = psql_on(
                primary_host,
                "SELECT coalesce(extract(epoch from replay_lag), 0) FROM pg_stat_replication;",
                database="postgres",
            ).strip()
            rl.progress(f"replica streaming replay_lag_s={lag}")
            try:
                if float(lag) < 30:
                    return
            except ValueError:
                pass
        time.sleep(15)
    raise StopAndAsk("replica did not reach streaming replication with replay lag under 30s")


def point_loadgen_at_replica(replica_private):
    rl.ssh(
        rl.LOAD,
        f"""
python3 - <<'PY'
from pathlib import Path
path = Path("/root/.pgpass")
lines = [line for line in path.read_text().splitlines() if line]
pw = None
for line in lines:
    parts = line.split(":")
    if len(parts) >= 5 and parts[2] == "social" and parts[3] == "bench":
        pw = parts[4]
        break
if not pw:
    raise SystemExit("missing bench pgpass")
want = "{replica_private}:5432:social:bench:" + pw
if want not in lines:
    lines.append(want)
path.write_text("\\n".join(lines) + "\\n")
path.chmod(0o600)
PY
""",
    )


def begin_job(host, job, script):
    remote = f"/var/tmp/pg-limit/jobs/{job}"
    local = ROOT / "results" / "jobs" / f"{job}.sh"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(script if script.endswith("\n") else script + "\n")
    rl.ensure_ssh_ip()
    rl.ssh(host, "command -v tmux >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y tmux")
    rl.ssh(host, f"mkdir -p {remote}")
    rl.scp_to(host, local, f"{remote}/script.sh")
    rl.ssh(
        host,
        f"""
set -e
tmux kill-session -t {job} 2>/dev/null || true
chmod 700 {remote}/script.sh
rm -f {remote}/exit {remote}/log
tmux new-session -d -s {job} 'bash {remote}/script.sh > {remote}/log 2>&1; echo $? > {remote}/exit'
""",
    )


def kill_jobs(host, jobs):
    script = " ; ".join(f"tmux kill-session -t {job} 2>/dev/null || true" for job in jobs)
    rl.ssh(host, script, check=False)


def wait_jobs(host, jobs, follows, timeout_s):
    begun = time.monotonic()
    seen = {job: 0 for job in jobs}
    pending = set(jobs)
    while pending:
        if time.monotonic() - begun > timeout_s:
            kill_jobs(host, jobs)
            raise RuntimeError(f"jobs still running after {timeout_s:.0f}s: {sorted(pending)}")
        time.sleep(10)
        for job in list(pending):
            follow = follows[job]
            try:
                probe = rl.ssh(
                    host,
                    f"""
if [ -f /var/tmp/pg-limit/jobs/{job}/exit ]; then echo STATUS DONE $(cat /var/tmp/pg-limit/jobs/{job}/exit); else
  if tmux has-session -t {job} 2>/dev/null; then echo STATUS UP; else echo STATUS DEAD; fi
fi
if [ -f {follow} ]; then tail -n +{seen[job] + 1} {follow}; fi
""",
                    check=False,
                )
            except RuntimeError:
                rl.ensure_ssh_ip()
                continue
            if probe.returncode != 0:
                rl.ensure_ssh_ip()
                continue
            lines = probe.stdout.splitlines()
            if not lines:
                continue
            status = lines[0].strip()
            for line in lines[1:]:
                text = line.rstrip()
                if text and ("progress" in text or "tps" in text or "ERROR" in text):
                    rl.progress(f"{job} {text}")
            seen[job] += len(lines) - 1
            if status.startswith("STATUS DONE"):
                code = int(status.split()[-1])
                if code != 0:
                    kill_jobs(host, jobs)
                    log = rl.ssh(host, f"tail -c 2000 {follow} 2>/dev/null || true", check=False).stdout
                    raise RuntimeError(f"{job} exited {code}\n{log}")
                pending.discard(job)
            elif status == "STATUS DEAD":
                time.sleep(2)
                again = rl.ssh(
                    host,
                    f"cat /var/tmp/pg-limit/jobs/{job}/exit 2>/dev/null || true",
                    check=False,
                ).stdout.strip()
                if again == "":
                    kill_jobs(host, jobs)
                    raise RuntimeError(f"{job} ended without an exit code")


def pgbench_script(run_id, side, host, files, seconds, rate, progress=10):
    remote = f"/var/tmp/pg-limit/{run_id}/{side}"
    rate_arg = f"--rate={int(rate)}" if rate is not None else ""
    file_args = " ".join(f"-f /opt/pg-limit/sql/{name}@{weight}" for name, weight in files)
    return f"""
set -e
mkdir -p {remote}
stdbuf -oL -eL pgbench -n -h {host} -p 5432 -U bench -d social \\
  -M prepared -c {rl.CLIENTS} -j {rl.JOBS} \\
  {file_args} \\
  -T {seconds} -P {progress} {rate_arg} \\
  --log --log-prefix={remote}/txlog \\
  > {remote}/summary.txt 2>&1
"""


def split_rates(offered):
    if offered is None:
        return None, None
    read_rate = int(round(int(offered) * 0.9))
    write_rate = int(offered) - read_rate
    if write_rate < 1:
        write_rate = 1
        read_rate = max(1, int(offered) - 1)
    return read_rate, write_rate


def start_replica_collectors(run_id, replica_host):
    remote = f"/var/tmp/pg-limit/{run_id}"
    rl.ssh(
        replica_host,
        f"""
mkdir -p {remote}
rm -f {remote}/*.pid
nohup vmstat 5 > {remote}/vmstat.txt 2>&1 & echo $! > {remote}/vmstat.pid
nohup iostat -x 5 > {remote}/iostat.txt 2>&1 & echo $! > {remote}/iostat.pid
""",
    )


def stop_replica_collectors(run_id, replica_host):
    remote = f"/var/tmp/pg-limit/{run_id}"
    rl.ssh(
        replica_host,
        f"""
for f in {remote}/*.pid; do
  [ -f "$f" ] || continue
  kill "$(cat "$f")" 2>/dev/null || true
done
""",
        check=False,
    )


def start_lag(run_id):
    remote = f"/var/tmp/pg-limit/{run_id}"
    rl.ssh(
        rl.DB,
        f"""
mkdir -p {remote}
nohup bash -c 'while true; do sudo -u postgres psql -d postgres -At -F, -c "SELECT floor(extract(epoch from clock_timestamp())), coalesce(extract(epoch from replay_lag),0), coalesce(extract(epoch from write_lag),0), coalesce(extract(epoch from flush_lag),0) FROM pg_stat_replication"; sleep 5; done' > {remote}/lag.csv 2>/dev/null & echo $! > {remote}/lag.pid
""",
    )


def lag_stats(text):
    replays = []
    writes = []
    flushes = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            replay, write, flush = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        replays.append(replay)
        writes.append(write)
        flushes.append(flush)
    if not replays:
        return None
    return {
        "n": len(replays),
        "replay_median": statistics.median(replays),
        "replay_max": max(replays),
        "write_max": max(writes),
        "flush_max": max(flushes),
    }


def side_window(paths, ramp_s):
    events = list(rl.iter_txlog(paths))
    if not events:
        return None
    t0 = min(event[0] for event in events)
    cut = t0 + ramp_s
    scored_lat, scored_exec, ramp_lat, ramp_exec = [], [], [], []
    scored_fail = ramp_fail = 0
    for ts, _script, time_us, lag, good in events:
        if not good:
            if ts < cut:
                ramp_fail += 1
            else:
                scored_fail += 1
            continue
        if ts < cut:
            ramp_lat.append(time_us)
            ramp_exec.append(time_us - lag)
        else:
            scored_lat.append(time_us)
            scored_exec.append(time_us - lag)
    return {
        "scored_lat": scored_lat,
        "scored_exec": scored_exec,
        "ramp_lat": ramp_lat,
        "ramp_exec": ramp_exec,
        "scored_fail": scored_fail,
        "ramp_fail": ramp_fail,
        "n_ok": len(scored_lat),
    }


def hit_delta(before, after):
    b_hit, b_read = rl.parse_db_stats(before)
    a_hit, a_read = rl.parse_db_stats(after)
    d_hit, d_read = a_hit - b_hit, a_read - b_read
    if d_hit + d_read == 0:
        return None
    return d_hit / (d_hit + d_read)


def measure_split(run_id, run_type, seconds, offered, replica_host, replica_private):
    dest = rl.RAW / run_id
    if (dest / "result.json").exists():
        rl.progress(f"reusing finished {run_id}")
        return json.loads((dest / "result.json").read_text())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    ramp = rl.RAMP_S if offered is not None else 0
    run_seconds = seconds + ramp
    read_rate, write_rate = split_rates(offered)
    rl.progress(
        f"start {run_id} type={run_type} seconds={seconds} ramp={ramp} "
        f"offered={offered} read_rate={read_rate} write_rate={write_rate}"
    )
    guard(run_id)
    primary_before = rl.psql_db(
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';"
    )
    replica_before = psql_on(
        replica_host,
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
    )
    (dest / "before_primary.txt").write_text(primary_before)
    (dest / "before_replica.txt").write_text(replica_before)
    read_job = f"p2-{run_id}-read"
    write_job = f"p2-{run_id}-write"
    read_script = pgbench_script(run_id, "read", replica_private, READ_FILES, run_seconds, read_rate)
    write_script = pgbench_script(run_id, "write", rl.DB_PRIVATE, WRITE_FILES, run_seconds, write_rate)
    follows = {
        read_job: f"/var/tmp/pg-limit/{run_id}/read/summary.txt",
        write_job: f"/var/tmp/pg-limit/{run_id}/write/summary.txt",
    }
    try:
        begin_job(rl.LOAD, read_job, read_script)
        begin_job(rl.LOAD, write_job, write_script)
        rl.start_collectors(run_id, all_pgbench=True)
        start_replica_collectors(run_id, replica_host)
        start_lag(run_id)
        wait_jobs(rl.LOAD, [read_job, write_job], follows, run_seconds + 900)
    finally:
        kill_jobs(rl.LOAD, [read_job, write_job])
        rl.stop_collectors(run_id)
        stop_replica_collectors(run_id, replica_host)
    primary_after = rl.psql_db(
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';"
    )
    replica_after = psql_on(
        replica_host,
        "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
    )
    (dest / "after_primary.txt").write_text(primary_after)
    (dest / "after_replica.txt").write_text(replica_after)
    rl.scp_from(rl.DB, f"/var/tmp/pg-limit/{run_id}", dest / "db")
    rl.scp_from(rl.LOAD, f"/var/tmp/pg-limit/{run_id}", dest / "loadgen")
    rl.scp_from(replica_host, f"/var/tmp/pg-limit/{run_id}", dest / "replica")
    return finalize_split(dest, run_id, run_type, seconds, offered, read_rate, write_rate, ramp)


def finalize_split(dest, run_id, run_type, seconds, offered, read_rate, write_rate, ramp):
    read_logs = sorted((dest / "loadgen" / "read").glob("txlog*"))
    write_logs = sorted((dest / "loadgen" / "write").glob("txlog*"))
    read_side = side_window(read_logs, ramp)
    write_side = side_window(write_logs, ramp)
    notes = [f"read_rate {read_rate}", f"write_rate {write_rate}", "clients 50+50", "jobs 8+8"]
    invalid = False
    if read_side is None or write_side is None:
        invalid = True
        notes.append("missing txlog")
        read_side = read_side or {"scored_lat": [], "scored_exec": [], "ramp_lat": [], "ramp_exec": [], "scored_fail": 0, "ramp_fail": 0, "n_ok": 0}
        write_side = write_side or {"scored_lat": [], "scored_exec": [], "ramp_lat": [], "ramp_exec": [], "scored_fail": 0, "ramp_fail": 0, "n_ok": 0}
    lat = sorted(read_side["scored_lat"] + write_side["scored_lat"])
    execs = sorted(read_side["scored_exec"] + write_side["scored_exec"])
    ramp_lat = sorted(read_side["ramp_lat"] + write_side["ramp_lat"])
    ramp_exec = sorted(read_side["ramp_exec"] + write_side["ramp_exec"])
    n_ok = read_side["n_ok"] + write_side["n_ok"]
    n_fail = read_side["scored_fail"] + write_side["scored_fail"]
    total_qps = n_ok / seconds if seconds else None
    read_qps = read_side["n_ok"] / seconds if seconds else None
    write_qps = write_side["n_ok"] / seconds if seconds else None
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
    lag = lag_stats(lag_path.read_text(errors="replace")) if lag_path else None
    primary_hit = hit_delta((dest / "before_primary.txt").read_text(), (dest / "after_primary.txt").read_text())
    replica_hit = hit_delta((dest / "before_replica.txt").read_text(), (dest / "after_replica.txt").read_text())
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
        "phase": "phase2",
        "scale": "100M",
        "workload": "mixed-8020-replica",
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


def measure_split_rerun(run_id, run_type, seconds, offered, replica_host, replica_private, attempts=3):
    last = None
    for attempt in range(1, attempts + 1):
        suffix = "" if attempt == 1 else f"-rerun{attempt}"
        last = measure_split(
            f"{run_id}{suffix}",
            run_type,
            seconds,
            offered,
            replica_host,
            replica_private,
        )
        if not last["invalid"]:
            return last
        rl.progress(f"{run_id} invalid, attempt {attempt}")
    return last


def latest_tps(text):
    matches = [rl.PROGRESS_RE.search(line) for line in text.splitlines()]
    matches = [match for match in matches if match]
    if not matches:
        return None
    return float(matches[-1].group(1)), float(matches[-1].group(2))


def split_warmup(replica_host, replica_private):
    run_id = "100m-replica-warmup"
    dest = rl.RAW / run_id
    if (dest / "warmup.json").exists():
        rl.progress(f"reusing warmup {run_id}")
        return json.loads((dest / "warmup.json").read_text())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    rl.progress(f"adaptive warmup {run_id} min {rl.WARMUP_MIN_S}s max {rl.WARMUP_MAX_S}s")
    read_job = "p2-100m-replica-warmup-read"
    write_job = "p2-100m-replica-warmup-write"
    begin_job(rl.LOAD, read_job, pgbench_script(run_id, "read", replica_private, READ_FILES, rl.WARMUP_MAX_S, None, progress=60))
    begin_job(rl.LOAD, write_job, pgbench_script(run_id, "write", rl.DB_PRIVATE, WRITE_FILES, rl.WARMUP_MAX_S, None, progress=60))
    rl.start_collectors(run_id, all_pgbench=True)
    start_replica_collectors(run_id, replica_host)
    windows = []
    prev = None
    stable = False
    capped = False
    started = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started
            guard(run_id)
            if elapsed > rl.WARMUP_MAX_S + 90:
                capped = True
                break
            time.sleep(20)
            read_text = rl.ssh(rl.LOAD, f"cat /var/tmp/pg-limit/{run_id}/read/summary.txt 2>/dev/null || true", check=False).stdout
            write_text = rl.ssh(rl.LOAD, f"cat /var/tmp/pg-limit/{run_id}/write/summary.txt 2>/dev/null || true", check=False).stdout
            read_now = latest_tps(read_text)
            write_now = latest_tps(write_text)
            if read_now is None or write_now is None:
                if elapsed > 90:
                    alive = rl.ssh(
                        rl.LOAD,
                        "for s in p2-100m-replica-warmup-read p2-100m-replica-warmup-write; do "
                        "if tmux has-session -t $s 2>/dev/null; then echo $s UP; else echo $s DOWN; fi; done",
                        check=False,
                    ).stdout
                    if "DOWN" in alive:
                        raise RuntimeError(f"warmup pgbench exited early\n{read_text[-1500:]}\n{write_text[-1500:]}")
                continue
            mark_s = min(read_now[0], write_now[0])
            if windows and mark_s <= windows[-1]["elapsed_s"]:
                continue
            stats = psql_on(
                replica_host,
                "SELECT blks_hit || ',' || blks_read FROM pg_stat_database WHERE datname = 'social';",
            ).strip()
            hit, read = [int(part) for part in stats.split(",")]
            interval_hit = None
            if prev is not None:
                d_hit, d_read = hit - prev[0], read - prev[1]
                if d_hit + d_read:
                    interval_hit = d_hit / (d_hit + d_read)
            prev = (hit, read)
            qps = read_now[1] + write_now[1]
            windows.append({"elapsed_s": mark_s, "qps": qps, "hit_ratio": interval_hit})
            rl.progress(f"warmup {run_id} t={mark_s:.0f}s qps={qps:.1f} hit={interval_hit}")
            if mark_s >= rl.WARMUP_MIN_S and len(windows) >= 3:
                tail = windows[-3:]
                if all(window["hit_ratio"] is not None for window in tail):
                    qvals = [window["qps"] for window in tail]
                    hits = [window["hit_ratio"] for window in tail]
                    qmean = sum(qvals) / 3
                    qspan = (max(qvals) - min(qvals)) / qmean if qmean else 1
                    hspan = max(hits) - min(hits)
                    if qspan <= rl.WARMUP_QPS_SPAN and hspan <= rl.WARMUP_HIT_SPAN:
                        stable = True
                        break
            if mark_s >= rl.WARMUP_MAX_S:
                capped = True
                break
    finally:
        kill_jobs(rl.LOAD, [read_job, write_job])
        rl.stop_collectors(run_id)
        stop_replica_collectors(run_id, replica_host)
    info = {
        "mode": "adaptive",
        "elapsed_s": time.monotonic() - started,
        "stable": stable,
        "capped": capped,
        "windows": windows,
    }
    (dest / "warmup.json").write_text(json.dumps(info, indent=2) + "\n")
    rl.progress(f"warmup done {run_id} seconds={info['elapsed_s']:.0f} stable={stable} capped={capped}")
    return info


def run_replica_limit(replica_host, replica_private):
    rl.PHASE = "phase2"
    rl.set_scale(100_000_000)
    rl.global_workload("mixed")
    rl.write_sql("eighty_twenty")
    warmup = split_warmup(replica_host, replica_private)
    ceiling = measure_split_rerun(
        "100m-replica-ceiling", "ceiling", 120, None, replica_host, replica_private, attempts=2
    )
    summary = {
        "prefix": "100m-replica",
        "scale": "100M",
        "workload": "mixed-8020-replica",
        "read_target": "replica",
        "write_target": "primary",
        "clients_per_side": rl.CLIENTS,
        "jobs_per_side": rl.JOBS,
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
        result = measure_split_rerun(
            f"100m-replica-search-{step:02d}", "search", 120, mid, replica_host, replica_private
        )
        if result["invalid"] or not result["passed"]:
            summary["search_failures"].append(result["row"])
            hi = mid
        else:
            best = result
            lo = mid
    summary["candidate_rate"] = None if best is None else best["rate"]
    summary["candidate"] = None if best is None else best["row"]
    if best is None:
        summary["stopped_reason"] = "no 2-minute search rate passed"
        return summary
    current = int(best["rate"])
    stepdowns = 0
    while current >= 1:
        results = []
        failed = False
        for i in (1, 2, 3):
            result = measure_split_rerun(
                f"100m-replica-confirm-r{current}-{i}",
                "confirm",
                300,
                current,
                replica_host,
                replica_private,
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
        rl.progress(f"replica confirm at {current} failed; stepping down to {nxt}")
        current = nxt
        stepdowns += 1
    summary["end_db_bytes"], summary["end_wal_bytes"] = rl.wal_and_db_bytes()
    return summary


def park_replica(replica_host):
    rl.ssh(replica_host, "systemctl stop postgresql || true", check=False)
    for _ in range(12):
        active = psql_on(
            rl.DB,
            "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'pg_limit_replica' AND active;",
            database="postgres",
        ).strip()
        if active == "0":
            break
        time.sleep(5)
    else:
        raise StopAndAsk("replication slot pg_limit_replica stayed active; not dropping it and not resizing")
    present = psql_on(
        rl.DB,
        "SELECT slot_name FROM pg_replication_slots WHERE slot_name = 'pg_limit_replica';",
        database="postgres",
    ).strip()
    if present == "pg_limit_replica":
        psql_on(rl.DB, "SELECT pg_drop_replication_slot('pg_limit_replica');", database="postgres")
    left = psql_on(rl.DB, "SELECT count(*) FROM pg_replication_slots;", database="postgres").strip()
    if left != "0":
        raise StopAndAsk(f"primary still has {left} replication slots; not resizing")
    mark("replica-parked")
    rl.progress("replica postgres stopped and replication slot dropped")


def size_price(slug):
    sizes = json.loads(rl.doctl(["compute", "size", "list", "-o", "json"]))
    for size in sizes:
        if size.get("slug") == slug and "lon1" in (size.get("regions") or []):
            return size
    return None


def resize_primary():
    before = droplet_detail(DB_ID)
    if before.get("name") != "pg-limit-db-dedi" or "pg-limit-test" not in (before.get("tags") or []):
        raise StopAndAsk("refusing to resize a droplet that is not the tagged primary")
    disk_before = droplet_disk(before)
    if disk_before is None:
        raise StopAndAsk("could not read the primary disk size; not resizing")
    chosen = size_price(BIGGER_SIZE)
    if chosen is None:
        raise StopAndAsk(f"{BIGGER_SIZE} is not available in lon1")
    guard("resize primary")
    already = before.get("size_slug") == BIGGER_SIZE
    if already:
        rl.progress(f"primary is already {BIGGER_SIZE}; checking disk, counts, and the 16 GB settings")
    else:
        rl.progress(
            f"resizing primary to {BIGGER_SIZE} ${chosen['price_hourly']}/hr disk stays {disk_before} GB"
        )
        rl.doctl(
            [
                "compute", "droplet-action", "resize", DB_ID,
                "--size", BIGGER_SIZE,
                "--resize-disk=false",
                "--wait",
            ]
        )
    after = droplet_detail(DB_ID)
    disk_after = droplet_disk(after)
    if disk_after != disk_before:
        raise StopAndAsk(
            f"resize changed the disk from {disk_before} GB to {disk_after} GB; stopping without a reload"
        )
    public, private = droplet_ips(after)
    update_state(
        {
            "DB_PUBLIC": public,
            "DB_PRIVATE": private,
            "DB_SIZE": BIGGER_SIZE,
            "DB_HOURLY": str(chosen["price_hourly"]),
        }
    )
    rl.load_state()
    wait_ssh(public)
    for _ in range(30):
        try:
            up = psql_on(public, "SELECT 1;", database="postgres").strip()
            if up == "1":
                break
        except RuntimeError:
            rl.ssh(public, "systemctl start postgresql || true", check=False)
            time.sleep(10)
    else:
        raise StopAndAsk("postgres did not start after the resize; the disk was not modified by this script")
    counts = rl.table_counts()
    (ROOT / "results" / "resize-counts.txt").write_text(counts + "\n")
    parsed = dict(line.split("|", 1) for line in counts.splitlines() if "|" in line)
    if int(parsed.get("users", "0")) < 10_000_000 or int(parsed.get("posts", "0")) < 100_000_000 or int(parsed.get("likes", "0")) < 200_000_000:
        raise StopAndAsk(f"row counts after resize are short of the loaded 100M data: {counts}")
    rl.ssh(
        public,
        "export PRIVATE="
        + private
        + """
python3 - <<'PY'
import os
from pathlib import Path
path = Path("/etc/postgresql/18/main/conf.d/pg-limit.conf")
repl = {
    "shared_buffers": "shared_buffers = 4GB",
    "effective_cache_size": "effective_cache_size = 12GB",
    "maintenance_work_mem": "maintenance_work_mem = 2GB",
    "listen_addresses": "listen_addresses = '" + os.environ["PRIVATE"] + "'",
}
lines = []
seen = set()
for line in path.read_text().splitlines():
    key = line.split("=", 1)[0].strip()
    if key in repl:
        lines.append(repl[key])
        seen.add(key)
    else:
        lines.append(line)
for key, value in repl.items():
    if key not in seen:
        lines.append(value)
path.write_text("\\n".join(lines) + "\\n")
PY
systemctl restart postgresql
systemctl is-active postgresql
""",
    )
    shown = psql_on(
        public,
        "SELECT name || '=' || setting FROM pg_settings WHERE name IN ('shared_buffers','effective_cache_size','maintenance_work_mem') ORDER BY 1;",
    )
    conf = rl.ssh(public, "cat /etc/postgresql/18/main/conf.d/pg-limit.conf").stdout
    (ROOT / "results" / "postgres-16gb.conf").write_text(conf)
    if "shared_buffers=524288" not in shown.replace(" ", ""):
        raise StopAndAsk(f"16 GB settings did not apply: {shown}")
    replica_hourly = float(state_values().get("REPLICA_HOURLY", "0.18452"))
    events = json.loads((ROOT / "results" / "billing-events.json").read_text())
    if not any("c5-8vcpu-16gb" in (row.get("note") or "") for row in events):
        add_billing(
            float(chosen["price_hourly"]) + 0.25 + replica_hourly,
            "primary c5-8vcpu-16gb plus replica plus load generator",
        )
    old_private = droplet_ips(before)[1]
    if old_private != private:
        rl.ssh(
            rl.LOAD,
            "export NEW="
            + private
            + " OLD="
            + old_private
            + """
python3 - <<'PY'
import os
from pathlib import Path
path = Path("/root/.pgpass")
lines = []
for line in path.read_text().splitlines():
    parts = line.split(":")
    if len(parts) >= 5 and parts[0] == os.environ["OLD"] and parts[2] == "social":
        parts[0] = os.environ["NEW"]
        lines.append(":".join(parts))
    elif line:
        lines.append(line)
path.write_text("\\n".join(lines) + "\\n")
path.chmod(0o600)
PY
""",
        )
    mark("resized")
    rl.progress(f"primary is {BIGGER_SIZE}; counts held; postgres retuned")
    return chosen


def run_bigger_box():
    rl.PHASE = "phase2"
    rl.set_scale(100_000_000)
    rl.global_workload("mixed")
    guard("100m-16gb")
    summary = rl.run_open_limit("100m-16gb", warmup="adaptive")
    summary["ram_bytes"] = 16 * GIB
    summary["size_slug"] = BIGGER_SIZE
    (ROOT / "results" / "summary-100m-16gb.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    rl.progress(f"100m-16gb confirmed={summary.get('confirmed_rate')} reason={summary.get('stopped_reason')}")
    return summary


def database_bytes(path):
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("database,"):
            return int(line.split(",")[1])
    return None


def passing_rows(summary):
    for attempt in summary.get("attempts") or []:
        if attempt.get("passed"):
            return attempt.get("rows") or []
    return []


def fmt_triple(rows, key, digits=3):
    values = []
    for row in rows:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        values.append(f"{float(raw):.{digits}f}")
    return " / ".join(values)


def users_of(rows):
    values = []
    for row in rows:
        if row.get("total_qps") not in (None, ""):
            values.append(str(int(round(float(row["total_qps"]) * 10))))
    return " / ".join(values)


def size_cell(db_bytes, ram_bytes):
    if not db_bytes or not ram_bytes:
        return ""
    return f"{db_bytes / GIB:.2f} GiB ({db_bytes / ram_bytes:.2f}× {ram_bytes / GIB:.0f} GiB)"


def nearest_failure(summary):
    confirmed = summary.get("confirmed_rate") or 0
    rows = []
    for row in summary.get("search_failures") or []:
        rate = row.get("target_rate")
        if rate not in (None, "") and int(rate) > int(confirmed):
            rows.append(row)
    for attempt in summary.get("attempts") or []:
        if attempt.get("passed"):
            continue
        for row in attempt.get("rows") or []:
            if row.get("pass_fail") == "fail":
                rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda row: int(row.get("target_rate") or 0))
    return rows[0]


def broke_sentence(summary, fallback):
    row = nearest_failure(summary)
    if row is None:
        return fallback
    return (
        f"Offered {row.get('target_rate')} failed with p95 {row.get('p95')} ms and p99 {row.get('p99')} ms "
        f"(run {row.get('run_id')}). {fallback}"
    )


def write_results(stopped):
    old = ""
    current = (ROOT / "results" / "RESULTS.md").read_text() if (ROOT / "results" / "RESULTS.md").exists() else ""
    marker = "<!-- method-history -->"
    if marker in current:
        old = current.split(marker, 1)[1].lstrip()
    else:
        old = current
    summaries = {}
    for name in ("1m-dedi", "10m-v2", "100m-v2", "100m-v2-write", "100m-replica", "100m-16gb"):
        path = ROOT / "results" / f"summary-{name}.json"
        if path.exists():
            summaries[name] = json.loads(path.read_text())
    ram8 = 8 * GIB
    ram16 = 16 * GIB
    sizes = {
        "1m-dedi": database_bytes(ROOT / "results" / "load-1m-dedi.txt"),
        "10m-v2": database_bytes(ROOT / "results" / "load-10m-v2.txt"),
        "100m-v2": database_bytes(ROOT / "results" / "load-100m-v2.txt"),
    }
    labels = [
        ("1m-dedi", "1M mixed", ram8, sizes.get("1m-dedi")),
        ("10m-v2", "10M mixed", ram8, sizes.get("10m-v2")),
        ("100m-v2", "100M mixed", ram8, sizes.get("100m-v2")),
        ("100m-v2-write", "100M write-only", ram8, sizes.get("100m-v2")),
        ("100m-replica", "100M replica", ram8, None),
        ("100m-16gb", "100M 8 vCPU / 16 GB", ram16, None),
    ]
    lines = [
        "<!-- phase2-report -->",
        "# Postgres single-database limit",
        "",
        "The published number is the offered open-loop rate. Latency is pgbench's logged time, with the first 30 seconds excluded. A run is published only when three separate 5-minute runs pass: p95 under 100 ms, p99 under 500 ms, and failed or skipped under 1%. Users are measured QPS times 10, one number per confirm. Cache hit ratio is the pg_stat_database delta over the scored run. On the replica row it is the replica's hit ratio, because that is where the reads ran.",
        "",
        "| Scale | Offered QPS | Measured QPS | Read QPS | Write QPS | Users | p50 ms | p95 ms | p99 ms | Database vs RAM | Cache hit | What broke first |",
        "| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    known = {
        "1m-dedi": "The next rate up failed on logged-time p95 while query execution stayed a few milliseconds and the cache hit ratio stayed 1.0. The closed-loop ceiling was about 97% CPU. Schedule lag broke first.",
        "10m-v2": "The working set no longer fit in RAM. Cache hit fell to about 0.977 and iowait rose to about 12%, against a 1.0 hit ratio and about 3% iowait at 1M. Logged p99 then blew out from schedule lag while execution stayed a few milliseconds.",
        "100m-v2": "The database was about 7.5 times RAM and the warmup cache hit was about 0.77. The next rate up crossed p95 of 100 ms on logged time while execution p99 stayed near 33 ms.",
    }
    for key, label, ram, start_bytes in labels:
        summary = summaries.get(key)
        if summary is None or not summary.get("confirmed_rate"):
            lines.append(f"| {label} | | | | | | | | | | | not confirmed |")
            continue
        rows = passing_rows(summary)
        db_bytes = start_bytes or summary.get("end_db_bytes")
        if key == "100m-16gb":
            ram = summary.get("ram_bytes") or ram
        broke = broke_sentence(summary, known.get(key, ""))
        lines.append(
            "| {label} | {offered} | {measured} | {read} | {write} | {users} | {p50} | {p95} | {p99} | {size} | {hit} | {broke} |".format(
                label=label,
                offered=summary["confirmed_rate"],
                measured=fmt_triple(rows, "total_qps"),
                read=fmt_triple(rows, "read_qps"),
                write=fmt_triple(rows, "write_qps"),
                users=users_of(rows),
                p50=fmt_triple(rows, "p50"),
                p95=fmt_triple(rows, "p95"),
                p99=fmt_triple(rows, "p99"),
                size=size_cell(db_bytes, ram),
                hit=fmt_triple(rows, "cache_hit_ratio", 6),
                broke=broke.replace("|", "/"),
            )
        )
    lines.extend(
        [
            "",
            "## Why QPS falls from 1M to 10M to 100M",
            "",
            "At 1M the database was 0.076 times the 8 GiB of RAM, the cache hit ratio on the confirms was 1.0, and iowait was about 3%. The box still had CPU left at the published rate. Failures above it were schedule lag: logged time includes the wait to start, and the execution time inside that log stayed a few milliseconds.",
            "",
            "At 10M the database was 0.75 times RAM. The hit ratio fell to about 0.977 and iowait rose to about 12%. The same queries still executed in a few milliseconds, but the disk wait made the offered rate miss its schedule, so logged p99 crossed the limit well below the 1M rate.",
            "",
            "At 100M the database was 7.54 times RAM (64,726,963,903 bytes after the index build and vacuum). Warmup cache hit was about 0.77. The published mixed rate is the highest offered rate whose logged p95 stayed under 100 ms. The next step up failed that p95 while execution p99 was still about 33 ms.",
            "",
            "## Replica",
            "",
            "One c5-4vcpu-8gb replica, the same size as the primary, used physical streaming replication. Profile and open-post ran on the replica. Like and new-post ran on the primary. Each side was its own pgbench process with 50 clients and 8 threads, because one pgbench process has one host. The read process was offered 90% of the rate and the write process the other 10%. Combined logged time, with each side's own first 30 seconds excluded, is the latency. Replay lag was sampled every 5 seconds from pg_stat_replication during the confirms; the median and max are in the raw lag.csv files and in the confirm notes.",
            "",
            "## Bigger box",
            "",
            "The replica was stopped and its replication slot was dropped before the resize, so the slot could not pin WAL and fill the primary disk. The primary was resized to c5-8vcpu-16gb with disk resize left off. Postgres was then set to shared_buffers 4GB, effective_cache_size 12GB, and maintenance_work_mem 2GB. The 100M rows were not reloaded. The mixed open-loop limit was measured on that primary alone, with the same 80/20 bounds frozen at the original 100,000,000 posts.",
            "",
            "## Droplets, time, and cost",
            "",
        ]
    )
    vals = state_values()
    events = []
    events_path = ROOT / "results" / "billing-events.json"
    if events_path.exists():
        events = json.loads(events_path.read_text())
    spent = rl.spend_usd()
    started = datetime(2026, 10, 5, 12, 41, 29, tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    lines.append(f"Elapsed time from 2026-10-05T12:41:29Z to this write: {hours:.2f} hours.")
    lines.append(f"Total cost over that interval: ${spent:.2f}.")
    lines.append("")
    lines.append("| Droplet | Size | Hourly |")
    lines.append("| --- | --- | ---: |")
    lines.append(f"| primary {vals.get('DB_NAME', '')} | {vals.get('DB_SIZE', '')} | ${vals.get('DB_HOURLY', '')} |")
    if vals.get("REPLICA_DESTROYED") == "yes":
        lines.append("| replica | c5-4vcpu-8gb | $0.18452, destroyed after the test |")
    else:
        lines.append(f"| replica | {vals.get('REPLICA_SIZE', '')} | ${vals.get('REPLICA_HOURLY', '')} |")
    if vals.get("LOAD_DESTROYED") == "yes":
        lines.append("| load generator | c-8 | $0.25000, destroyed after the test |")
    else:
        lines.append(f"| load generator | {vals.get('LOAD_SIZE', '')} | ${vals.get('LOAD_HOURLY', '')} |")
    lines.append("")
    lines.append("Rate changes:")
    lines.append("")
    for event in events:
        lines.append(f"- {event['at']}: ${event['hourly']:.5f}/hr, {event.get('note', '')}")
    lines.append("")
    if stopped:
        lines.extend(["## Stopped", "", stopped, ""])
    lines.extend(["## Method history", "", marker, "", old])
    (ROOT / "results" / "RESULTS.md").write_text("\n".join(lines).rstrip() + "\n")


def sync_raw():
    hosts = [("db", rl.DB), ("loadgen", rl.LOAD)]
    replica = state_values().get("REPLICA_PUBLIC")
    if replica:
        hosts.append(("replica", replica))
    for _label, host in hosts:
        try:
            listing = rl.ssh(host, "ls -1 /var/tmp/pg-limit 2>/dev/null || true", check=False).stdout
        except RuntimeError:
            rl.ensure_ssh_ip()
            continue
        for name in listing.split():
            if name == "jobs" or (rl.RAW / name).exists():
                continue
            rl.progress(f"copying missing raw {name} from {host}")
            rl.scp_from(host, f"/var/tmp/pg-limit/{name}", rl.RAW / name)


def destroy_one(droplet_id, expected_name):
    if str(droplet_id) == DB_ID:
        raise StopAndAsk("refusing to destroy the primary")
    detail = droplet_detail(droplet_id)
    if detail.get("name") != expected_name or "pg-limit-test" not in (detail.get("tags") or []):
        raise StopAndAsk(f"refusing to destroy {droplet_id}; name={detail.get('name')} tags={detail.get('tags')}")
    rl.doctl(["compute", "droplet", "delete", str(droplet_id), "--force"])
    rl.progress(f"destroyed {expected_name} {droplet_id}")


def destroy_extras():
    vals = state_values()
    replica_id = vals.get("REPLICA_ID")
    if replica_id and vals.get("REPLICA_DESTROYED") != "yes":
        if "replica-parked" not in done_set():
            raise StopAndAsk("refusing to destroy the replica while its slot may still be pinning WAL")
        destroy_one(replica_id, REPLICA_NAME)
        update_state({"REPLICA_DESTROYED": "yes"})
        add_billing(float(vals.get("DB_HOURLY", "0.36905")) + 0.25, "primary plus load generator, replica destroyed")
    vals = state_values()
    if vals.get("LOAD_ID") and vals.get("LOAD_DESTROYED") != "yes":
        destroy_one(vals["LOAD_ID"], "pg-limit-loadgen")
        update_state({"LOAD_DESTROYED": "yes"})
        add_billing(float(state_values().get("DB_HOURLY", "0.36905")), "primary only; load generator destroyed")


def write_lessons():
    text = """# Lessons

Method problems from this project, the cause, and the fix.

## Hot key

The first workload drew post ids with a zipfian 1.5 distribution and maintained the like count with `count(*)`. Almost every like write landed on the same few posts, and those writes serialized. There was no confirmed rate.

The like write is now one statement: insert the like, then add one to `posts.like_count`. Reads return that column and do not touch `likes`. Picks are 80/20: 80% uniform in the newest fifth of the loaded ids, 20% uniform over all loaded ids. Those bounds stay at the ids that were loaded. They do not move as the benchmark inserts new rows.

## Double-counted schedule lag

pgbench 18's logged `time` is measured from the scheduled start. It already includes `schedule_lag`. The first ramp-exclusion scoring added `schedule_lag` on top of `time`, so the wait was counted twice. On `1m-ramp-search-01`, scored p99 was 358.983 ms with lag added and 181.306 ms from logged time alone.

Pass/fail latency is logged time only. `schedule_lag` is still in the raw log. Execution time is logged time minus lag, and it is reported so a lag failure can be told apart from a slow query. `--latency-limit` is not used.

## pgbench thread count

Early runs used `-j 4` on the 8-vCPU load generator. A machine-wide CPU average can sit near 15% while one thread is saturated, so the average could not show that. Probes used `-j 4` and script checks used `-j 2`. Every warmup, ceiling, search, and confirm after the correction uses `-j 8`.

A run is invalid when any load-generator core's time-average, or any pgbench thread's time-average, is above 70%. The first sample is dropped. Missing per-core or per-thread samples also invalidate the run. Machine-wide average is recorded and is not the pass/fail. Saved mpstat from before this collector was machine-wide only, so older runs cannot be reclassified.

## Shared CPU steal

The first database droplet was a shared-CPU size. After the time-only scoring fix, every 1M run was invalid because steal averaged 5.2% to 8.9%. Latency and the load-generator threads were inside their limits. The search was stopped and the database was moved to a dedicated c5-4vcpu-8gb droplet. Steal above 5% on a database host still invalidates a run.

## Startup ramp

With `--rate`, the first time each client runs each script, pgbench does synchronous `PQprepare` inside the timed transaction. The Poisson scheduler falls behind during that burst. On the inspected ramp, the first 10 seconds had lag p99 around 400 ms while execution p99 stayed near 14 ms. By 30–40 seconds the mean was near the steady rate when the rate was sustainable.

The first 30 seconds stay in the raw log and their p99 is reported separately. They are excluded from the pass/fail percentiles and the error rate. Scored QPS is successes in the remaining window divided by the scored seconds.

## SSH drops

A multi-hour `UPDATE` or `count(*)` run inside an SSH session died when the session timed out. The `UPDATE` that set `like_count = 2` had committed; the indexes had not been built yet. A later count sat in a dead SSH session for hours.

Long steps run in tmux through `remote_job`. The local process polls an exit file. A dropped SSH session retries after refreshing the firewall rule and does not kill the remote step. A finished exit code of 0 is kept so a poll does not start a second copy.

## Warmup left running

The 10M adaptive warmup started pgbench for up to 40 minutes and did not stop it when the windows became stable, around 10 minutes. It kept running until the 40-minute timer and overlapped the first confirm at offered 13,309. That confirm is not a clean pass. The published 12,643 confirms started after the leftover process had exited.

The warmup pgbench now runs in tmux and is killed in a `finally` block when the warmup function returns.

## Firewall IP

SSH was allowed only from the public IPv4 address captured at setup, as a /32. When that address changed, SSH timed out and the benchmark looked stuck. Postgres was never open to the public internet.

`ensure_ssh_ip()` reads the current public address, adds the new /32, then removes the old SSH /32. It logs the change in `results/commands.log`. Port 5432 stays limited to private 10.130 addresses: the load generator, and the replica once that droplet exists.

## Identity and the loader host

The first setup on the dedicated droplet created `posts.id` as a plain bigint, so a new post could not omit the id and the sequence check came back empty. `posts.id` is `GENERATED BY DEFAULT AS IDENTITY`. `COPY` of explicit ids does not advance the sequence, so the load sets it to the loaded maximum.

The loader's default host was the original private address. After the database moved, `COPY` failed with no route to host. The loader takes the current private address.

## Replication slot

A replication slot pins WAL until the replica consumes it. Leaving the slot in place while the replica is stopped can fill the primary disk and threaten the loaded data. The replica is stopped and `pg_limit_replica` is dropped before the resize, and the resize does not proceed while any slot remains.
"""
    (ROOT / "LESSONS.md").write_text(text)


def write_harness():
    text = """# Harness

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
"""
    (ROOT / "HARNESS.md").write_text(text)


def run_tests():
    require_write_only()
    init_billing()
    rl.load_state()
    rl.ensure_ssh_ip()
    if "replica-limit" not in done_set():
        _replica_id, replica_public, replica_private = ensure_replica_droplet()
        allow_postgres_from(replica_private)
        install_replica_packages(replica_public)
        if not streaming(rl.DB, replica_public):
            ensure_replicator(rl.DB)
            basebackup(rl.DB_PRIVATE, replica_public)
            wait_streaming(rl.DB, replica_public)
        point_loadgen_at_replica(replica_private)
        ensure_replica_hba(replica_public)
        mark("replica-streaming")
        summary = run_replica_limit(replica_public, replica_private)
        (ROOT / "results" / "summary-100m-replica.json").write_text(
            json.dumps(summary, indent=2, default=str) + "\n"
        )
        if summary.get("stopped_reason") or not summary.get("confirmed_rate"):
            park_replica(replica_public)
            raise StopAndAsk(
                "the replica limit did not confirm after the step-down limit: "
                f"{summary.get('stopped_reason')}"
            )
        mark("replica-limit")
    vals = state_values()
    replica_public = vals["REPLICA_PUBLIC"]
    if "replica-parked" not in done_set():
        park_replica(replica_public)
    if "resized" not in done_set():
        resize_primary()
    if "16gb-limit" not in done_set():
        summary = run_bigger_box()
        if summary.get("stopped_reason") or not summary.get("confirmed_rate"):
            raise StopAndAsk(
                "the 16 GB limit did not confirm after the step-down limit: "
                f"{summary.get('stopped_reason')}"
            )
        mark("16gb-limit")


def main():
    if other_phase2():
        raise SystemExit("phase2 is already running")
    try:
        run_tests()
    except StopAndAsk as exc:
        rl.progress(f"STOP AND ASK: {exc}")
        (ROOT / "results" / "phase2-blocked.txt").write_text(str(exc) + "\n")
        try:
            sync_raw()
        except Exception as copy_exc:
            rl.progress(f"raw copy failed: {copy_exc}")
        write_results(str(exc))
        print(f"STOP AND ASK: {exc}", flush=True)
        return
    sync_raw()
    write_results(None)
    destroy_extras()
    write_results(None)
    write_lessons()
    write_harness()
    rl.progress("phase 2 finished; replica and load generator destroyed; primary kept")
    print("PHASE2 DONE", flush=True)


if __name__ == "__main__":
    main()
