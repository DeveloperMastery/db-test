#!/usr/bin/env python3
"""Stream users, posts, and likes into Postgres with COPY. No indexes."""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOST = "10.130.0.3"
ALPHABET = "abcdefghijklmnopqrstuvwxyz "
BODIES = []
for length in range(80, 201):
    BODIES.append((ALPHABET * ((length // len(ALPHABET)) + 1))[:length])

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def ts(offset):
    return (BASE + timedelta(seconds=int(offset))).strftime("%Y-%m-%d %H:%M:%S+00")


def psql_copy(sql, chunks):
    proc = subprocess.Popen(
        ["psql", "-h", HOST, "-U", "bench", "-d", "social", "-v", "ON_ERROR_STOP=1", "-c", sql],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for chunk in chunks:
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read()
    out = proc.stdout.read()
    code = proc.wait()
    if code != 0:
        sys.stderr.write(out)
        sys.stderr.write(err)
        raise SystemExit(f"COPY failed: {sql}")


def load_users(start_post, end_post, batch):
    first = (start_post - 1) // 10 + 1
    last = (end_post - 1) // 10 + 1
    print(f"users {first}..{last}", flush=True)
    started = time.perf_counter()

    def chunks():
        buf = []
        for uid in range(first, last + 1):
            buf.append(f"{uid}\tuser{uid}\t{ts(uid)}\n")
            if uid % 1_000_000 == 0:
                print(f"users_progress {uid}", flush=True)
            if len(buf) >= batch:
                yield "".join(buf)
                buf = []
        if buf:
            yield "".join(buf)

    psql_copy("COPY users (id, username, created_at) FROM STDIN", chunks())
    elapsed = time.perf_counter() - started
    print(f"users_seconds {elapsed:.3f}", flush=True)
    return elapsed


def load_posts(start_post, end_post, batch):
    print(f"posts {start_post}..{end_post}", flush=True)
    started = time.perf_counter()

    def chunks():
        buf = []
        for pid in range(start_post, end_post + 1):
            uid = (pid - 1) // 10 + 1
            buf.append(f"{pid}\t{uid}\t{BODIES[pid % 121]}\t{ts(pid)}\n")
            if pid % 1_000_000 == 0:
                print(f"posts_progress {pid}", flush=True)
            if len(buf) >= batch:
                yield "".join(buf)
                buf = []
        if buf:
            yield "".join(buf)

    psql_copy("COPY posts (id, user_id, body, created_at) FROM STDIN", chunks())
    elapsed = time.perf_counter() - started
    print(f"posts_seconds {elapsed:.3f}", flush=True)
    return elapsed


def load_likes(start_post, end_post, n_users, batch):
    print(f"likes for posts {start_post}..{end_post}", flush=True)
    started = time.perf_counter()

    def chunks():
        buf = []
        for pid in range(start_post, end_post + 1):
            created = ts(pid + 60)
            if pid % 1_000_000 == 0:
                print(f"likes_progress {pid}", flush=True)
            for k in (0, 1):
                uid = (pid + k) % n_users + 1
                buf.append(f"{pid}\t{uid}\t{created}\n")
            if len(buf) >= batch:
                yield "".join(buf)
                buf = []
        if buf:
            yield "".join(buf)

    psql_copy("COPY likes (post_id, user_id, created_at) FROM STDIN", chunks())
    elapsed = time.perf_counter() - started
    print(f"likes_seconds {elapsed:.3f}", flush=True)
    return elapsed


def main():
    global HOST
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-post", type=int, required=True)
    parser.add_argument("--end-post", type=int, required=True)
    parser.add_argument("--batch", type=int, default=100000)
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args()
    HOST = args.host
    n_users = args.end_post // 10
    # Likes reference users that exist at this scale. Growing in place keeps
    # earlier users, and new posts only reference users up to end_post/10.
    load_users(args.start_post, args.end_post, args.batch)
    load_posts(args.start_post, args.end_post, args.batch)
    load_likes(args.start_post, args.end_post, n_users, args.batch)
    print("load_done", flush=True)


if __name__ == "__main__":
    main()
