<!-- phase2-report -->
# Postgres single-database limit

The published number is the offered open-loop rate. Latency is pgbench's logged time, with the first 30 seconds excluded. A run is published only when three separate 5-minute runs pass: p95 under 100 ms, p99 under 500 ms, and failed or skipped under 1%. Users are measured QPS times 10, one number per confirm. Cache hit ratio is the pg_stat_database delta over the scored run. On the replica row it is the replica's hit ratio, because that is where the reads ran.

| Scale | Offered QPS | Measured QPS | Read QPS | Write QPS | Users | p50 ms | p95 ms | p99 ms | Database vs RAM | Cache hit | What broke first |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1M mixed | 21213 | 21183.663 / 21191.147 / 21206.063 | 19065.850 / 19068.500 / 19084.847 | 2117.813 / 2122.647 / 2121.217 | 211837 / 211911 / 212061 | 0.549 / 0.498 / 0.521 | 2.319 / 2.053 / 2.278 | 6.808 / 6.173 / 9.773 | 0.61 GiB (0.08× 8 GiB) | 1.000000 / 1.000000 / 1.000000 | Offered 22330 failed with p95 40.695 ms and p99 624.045 ms (run 1m-dedi-confirm-r22330-1). The next rate up failed on logged-time p95 while query execution stayed a few milliseconds and the cache hit ratio stayed 1.0. The closed-loop ceiling was about 97% CPU. Schedule lag broke first. |
| 10M mixed | 12643 | 12630.797 / 12632.660 / 12634.570 | 11367.150 / 11370.140 / 11364.900 | 1263.647 / 1262.520 / 1269.670 | 126308 / 126327 / 126346 | 0.430 / 0.420 / 0.422 | 2.651 / 1.706 / 1.875 | 231.670 / 4.772 / 5.856 | 6.04 GiB (0.75× 8 GiB) | 0.976867 / 0.976203 / 0.976820 | Offered 13309 failed with p95 2820.148 ms and p99 7491.951 ms (run 10m-v2-confirm-r13309-2). The working set no longer fit in RAM. Cache hit fell to about 0.977 and iowait rose to about 12%, against a 1.0 hit ratio and about 3% iowait at 1M. Logged p99 then blew out from schedule lag while execution stayed a few milliseconds. |
| 100M mixed | 8505 | 8490.063 / 8509.113 / 8500.810 | 7641.933 / 7660.617 / 7651.290 | 848.130 / 848.497 / 849.520 | 84901 / 85091 / 85008 | 1.551 / 1.325 / 1.275 | 87.743 / 76.046 / 30.476 | 264.286 / 295.279 / 155.980 | 60.28 GiB (7.54× 8 GiB) | 0.762145 / 0.759613 / 0.757734 | Offered 8859 failed with p95 128.708 ms and p99 271.921 ms (run 100m-v2-search-05). The database was about 7.5 times RAM and the warmup cache hit was about 0.77. The next rate up crossed p95 of 100 ms on logged time while execution p99 stayed near 33 ms. |
| 100M write-only | 1522 | 1521.303 / 1519.023 / 1520.903 | 0.000 / 0.000 / 0.000 | 1521.303 / 1519.023 / 1520.903 | 15213 / 15190 / 15209 | 3.409 / 3.786 / 3.959 | 35.101 / 44.932 / 75.674 | 159.892 / 176.521 / 200.612 | 60.28 GiB (7.54× 8 GiB) | 0.876582 / 0.876426 / 0.876680 | Offered 1,603 failed on the third confirm: logged p99 1,098.702 ms, p95 63.563 ms, scored execution p99 53.321 ms. CPU averaged 25% and iowait 33% (disk util 60%). Disk wait showed up as schedule lag. |
| 100M all reads on replica | 7735 | 7729.667 / 7740.023 / 7732.813 | 6957.977 / 6966.263 / 6960.160 | 771.690 / 773.760 / 772.653 | 77297 / 77400 / 77328 | 1.039 / 1.035 / 1.034 | 4.194 / 4.678 / 4.183 | 16.333 / 33.305 / 17.179 | 62.98 GiB (7.87× 8 GiB) | 0.634215 / 0.633442 / 0.632854 | Offered 7,985 failed logged p95 208.233 ms and p99 666.076 ms; execution p99 was 21.947 ms. Replay lag max on that run was 193.808 s. The replica fell behind. |
| 100M 8 vCPU / 16 GB | 30417 | 30427.967 / 30387.513 / 30395.783 | 27379.827 / 27344.157 / 27358.113 | 3048.140 / 3043.357 / 3037.670 | 304280 / 303875 / 303958 | 1.719 / 1.155 / 1.107 | 27.015 / 16.750 / 7.604 | 195.079 / 164.834 / 15.349 | 63.31 GiB (3.96× 16 GiB) | 0.775168 / 0.773064 / 0.771004 | Offered 31,466 failed logged p99 508.417 ms (p95 38.235 ms). Scored execution p99 was 5.158 ms. CPU averaged 85% and iowait 5.9%. Schedule lag broke first. |
| 100M mixed recheck | 8505 | 8502.667 / 8496.143 / 8499.060 | 7650.700 / 7648.090 / 7649.037 | 851.967 / 848.053 / 850.023 | 85027 / 84961 / 84991 | 1.903 / 2.134 / 2.603 | 19.127 / 47.754 / 82.028 | 56.132 / 236.280 / 214.876 | 63.40 GiB (7.93× 8 GiB) | 0.664877 / 0.663058 / 0.662609 | Three 5-minute confirms at 8,505 passed after the resize back to 8 GB. 8,505 stands. |
| 100M split reads | 15651 | 15640.927 / 15644.940 / 15645.443 | 14078.500 / 14081.187 / 14081.100 | 1562.427 / 1563.753 / 1564.343 | 156409 / 156449 / 156454 | 1.358 / 1.520 / 1.272 | 21.900 / 64.110 / 23.106 | 62.222 / 376.903 / 67.051 | 63.58 GiB (7.95× 8 GiB) | 0.595826 / 0.594255 / 0.593216 | Offered 16396 failed with p95 2704.073 ms and p99 5704.418 ms (run 100m-split-search-04). Reads were split 45/45 across primary and replica. Confirm replay lag max was 0.036 s, 0.018 s, and 0.008 s. |

## Why QPS falls from 1M to 10M to 100M

At 1M the database was 0.076 times the 8 GiB of RAM, the cache hit ratio on the confirms was 1.0, and iowait was about 3%. The box still had CPU left at the published rate. Failures above it were schedule lag: logged time includes the wait to start, and the execution time inside that log stayed a few milliseconds.

At 10M the database was 0.75 times RAM. The hit ratio fell to about 0.977 and iowait rose to about 12%. The same queries still executed in a few milliseconds, but the disk wait made the offered rate miss its schedule, so logged p99 crossed the limit well below the 1M rate.

At 100M the database was 7.54 times RAM (64,726,963,903 bytes after the index build and vacuum). Warmup cache hit was about 0.77. The published mixed rate is the highest offered rate whose logged p95 stayed under 100 ms. The next step up failed that p95 while execution p99 was still about 33 ms.

## Replica

One c5-4vcpu-8gb replica, the same size as the primary, used physical streaming replication. Profile and open-post ran on the replica. Like and new-post ran on the primary. Each side was its own pgbench process with 50 clients and 8 threads, because one pgbench process has one host. The read process was offered 90% of the rate and the write process the other 10%. Combined logged time, with each side's own first 30 seconds excluded, is the latency. Replay lag was sampled every 5 seconds from pg_stat_replication. On the three confirms the replay-lag max was 0.274 s, 0.101 s, and 0.026 s, and the median stayed near 0.003 s. The failing search at offered 7,985 had a replay-lag max of 193.808 s. Those samples are in `results/raw/100m-replica-*/db/lag.csv`.

## Bigger box

The replica was stopped and its replication slot was dropped before the resize, so the slot could not pin WAL and fill the primary disk. The primary was resized to c5-8vcpu-16gb with disk resize left off. Postgres was then set to shared_buffers 4GB, effective_cache_size 12GB, and maintenance_work_mem 2GB. The 100M rows were not reloaded. The mixed open-loop limit was measured on that primary alone, with the same 80/20 bounds frozen at the original 100,000,000 posts. The next rate up, offered 31,466, failed logged p99 at 508.417 ms while scored execution p99 stayed at 5.158 ms and CPU averaged 85%.


## Write-only confirms and the replica

The three write-only confirms at offered 1,522 ran with no replica attached and no base backup in progress. pgbench for confirm 1 started at 2026-10-06T15:38:33Z and the scored run was done at 2026-10-06T15:46:56Z. Confirm 2 started at 2026-10-06T15:49:15Z and was done at 2026-10-06T15:57:41Z. Confirm 3 started at 2026-10-06T16:00:00Z and was done at 2026-10-06T16:08:32Z. The summary was written at 2026-10-06T16:10:53Z. The replica droplet was created at 2026-10-06T16:11:15Z and existed at 2026-10-06T16:11:57Z. pg_basebackup started at 2026-10-06T16:14:03Z and finished at 2026-10-06T16:34:30Z. The write-only limit was not rerun.

## Why all-reads-on-replica warmed up near 16,500 and confirmed 7,735

The warmup and the ceiling were closed loop: two pgbench processes, no `--rate`. At 600 seconds the warmup's combined progress rate was 17,963 queries per second, and the replica cache hit was about 0.64. The ceiling measured 15970.367 QPS with logged p95 20.285 ms and p99 26.735 ms. Its notes record replay lag max 184.544 seconds. Closed loop has no schedule lag. Each side runs as fast as 50 clients allow, so the write side was about 3,500 QPS rather than 10% of the total, and logged time is the query time. The replica kept serving those reads while it fell minutes behind on WAL.

The first open-loop search, offered 7985, failed with logged p95 208.233 ms and p99 666.076 ms. Execution p99 in that run's notes is inside `read_rate 7186; write_rate 799; clients 50+50; jobs 8+8; replay_lag_median_s 117.250 replay_lag_max_s 193.808 write_lag_max_s 0.736 flush_lag_max_s 0.736 lag_n 31; loadgen_core_max 9.68; pgbench_thread_max 4.44; exec_p99 21.947; ramp_30s_p99 4535.208 ramp_exec_p99 43.075; primary_disk_util 51.12; replica_disk_util 56.72; primary_hit 0.875322; primary_cpu 15.39 primary_iowait 20.29; replica_cpu 62.84 replica_iowait 29.81`. Logged time already includes schedule lag, so a rate the clients cannot start on time fails even when the query itself is still tens of milliseconds. That search started after the ceiling, which had left replay lag at about 184 seconds, and the search's own replay lag median stayed above 100 seconds. The three confirms at 7,735 then ran with replay lag max 0.274 s, 0.101 s, and 0.026 s and logged p95 near 4 ms. 7,735 is the rate that passed while the replica was caught up. The closed-loop 16,500 figure is a different mix and a different latency rule.

## Why 8 vCPU / 16 GB was 3.6×

30,417 / 8,505 = 3.58. On the 8 GB box the published 100M mixed confirms had cache hit 0.762 / 0.760 / 0.758 and the next rate, offered 8,859, failed logged p95 at 128.708 ms while execution p99 stayed near 33 ms. On 16 GB the three confirms at 30,417 had cache hit 0.775 / 0.773 / 0.771, CPU about 82–86%, iowait about 5.5–7.3%, and scored execution p99 about 5.5–5.8 ms. Offered 31,466 then failed logged p99 at 508.417 ms with scored execution p99 5.158 ms. The 16 GB box ran out of schedule, at about 85% CPU. The 8 GB box ran out of logged p95 while queries were still waiting on disk.

The hot 20% is the newest fifth of the loaded ids, which is where 80% of the draws land: posts 20,000,000 of 105,826,440 (0.189 of the posts heap and indexes), users 2,000,000 of 10,000,000 (0.200), and likes on those posts. Loaded likes were 200,000,000. Likes above that are 23,317,566, and 80% of like writes hit the hot posts, so the hot like estimate is 58654053 rows, 0.263 of the likes heap and indexes.

Measured sizes: posts heap 38.87 GiB plus indexes 5.74 GiB, users heap 0.56 GiB plus indexes 0.21 GiB, likes heap 10.86 GiB plus indexes 7.05 GiB. The hot slice is 8.43 GiB of posts, 0.15 GiB of users, and 4.70 GiB of likes, 13.29 GiB together.

OS page cache after `shared_buffers` is about 6.00 GiB on the 8 GB box (8 GiB minus 2 GiB) and about 12.00 GiB on the 16 GB box (16 GiB minus 4 GiB). The hot slice does not fit in 8 GiB of RAM, and it is larger than the page cache left on that box. On the 16 GB machine it fits in 16 GiB of RAM and is just over the page cache left after `shared_buffers`. `shared_buffers` itself is 2 GiB and 4 GiB, so the full hot slice was never inside `shared_buffers` on either box. The whole database stayed near 63 GiB, several times either machine.

## Droplets, time, and cost

Elapsed time from 2026-10-05T12:41:29Z to this write: 31.61 hours.
Total cost over that interval: $13.96.

| Droplet | Size | Hourly |
| --- | --- | ---: |
| primary pg-limit-db-dedi | c5-8vcpu-16gb | $0.36905 |
| replica | c5-4vcpu-8gb | $0.18452, destroyed after the test |
| load generator | c-8 | $0.25000, destroyed after the test |

Rate changes:

- 2026-10-05T12:41:29Z: $0.32143/hr, shared database plus load generator
- 2026-10-05T19:18:44Z: $0.43452/hr, dedicated c5-4vcpu-8gb database plus load generator
- 2026-10-06T16:11:57Z: $0.61904/hr, primary c5-4 plus replica c5-4 plus load generator
- 2026-10-06T19:09:13Z: $0.80357/hr, primary c5-8vcpu-16gb plus replica plus load generator
- 2026-10-06T20:18:06Z: $0.61905/hr, primary plus load generator, replica destroyed
- 2026-10-06T20:18:07Z: $0.36905/hr, primary only; load generator destroyed


Updated cost after this round, 2026-10-07T15:15:26Z: 50.57 hours from 2026-10-05T12:41:29Z, $22.29 total. The primary is c5-4vcpu-8gb again at $0.18452/hr. The primary, the load generator, and the replica stay up for the case study.

- 2026-10-06T22:06:55Z: $0.18452/hr, primary c5-4vcpu-8gb only, before the load generator returned

- 2026-10-06T22:09:32Z: $0.43452/hr, primary c5-4vcpu-8gb plus load generator

- 2026-10-07T13:59:51Z: $0.61904/hr, primary c5-4 plus replica c5-4 plus load generator
- 2026-10-13T16:28:00Z: $0/hr, primary, load generator, and replica destroyed

Destroyed on 2026-10-13T16:28:00Z. Cost from 2026-10-05T12:41:29Z through shutdown: $112.19. Of that, the dedicated primary was $35.45, the load generators $48.48, the replicas $27.78, and the first shared database $0.47. DigitalOcean's October invoice preview had posted $102.13 of these droplet lines and had not yet included the latest hours.

## Method history

<!-- method-history -->

# 1M posts

Three 1M attempts are recorded here. The first used zipfian 1.5 and a live `count(*)` of likes, and it has no confirmed rate. The second stores `posts.like_count`, draws ids with an 80/20 rule, and confirmed an offered rate of 13,919 queries per second. The third keeps that workload and drops the first 30 seconds of every open-loop run from the percentile and the error rate. That third attempt did not confirm a rate. 10M was not started. The droplets are still up.

## First attempt: zipfian and a live like count

This is the checkpoint after the first 1M mixed workload. 10M, 100M, the write-only test, the replica, and the resize have not been run. The droplets are still up. Nothing has been destroyed.

There is no confirmed queries-per-second number for this attempt. The method requires three separate 5-minute open-loop runs, all passing. Eleven 5-minute runs, from 561 down to 332 requests per second, all failed the p95 limit. I stopped the step-down at the start of a 315 run. That run was killed and is not a result. `results/raw/1m-mixed-confirm-r315-1/` contains only the before-snapshots.

## What the database was

| | |
|---|---|
| Region | lon1 (London). `c-8` is not sold in Frankfurt. |
| Database | `pg-limit-db`, id 606292659, Basic `s-4vcpu-8gb`, 4 vCPU, 8 GB RAM, 160 GB SSD, $0.07143/hour |
| Load generator | `pg-limit-loadgen`, id 606292658, CPU-Optimized `c-8`, 8 vCPU, 16 GB RAM, 100 GB SSD, $0.25000/hour |
| Image | `ubuntu-26-04-x64` |
| VPC | `pg-limit-test`, `10.130.0.0/20`, uuid `fd199783-a389-4502-a428-7d5af216c5b9`. DigitalOcean returned 404 when tagging the VPC. The name is the only marker. |
| Firewall | `pg-limit-test`, tagged. TCP 22 from `5.77.132.172/32`. TCP 5432 from `10.130.0.2/32` only. |
| Postgres | 18.6 (Ubuntu 18.6-1.pgdg26.04+2), PGDG apt repo. Listening on `10.130.0.3:5432` only. |
| Created | both droplets at 2026-10-05T12:41:29Z |

Postgres settings that were in effect: `shared_buffers` 2 GB, `effective_cache_size` 6 GB, `random_page_cost` 1.1, `max_wal_size` 8 GB, `checkpoint_timeout` 15 min, `checkpoint_completion_target` 0.9, `max_connections` 200, `work_mem` 16 MB, `maintenance_work_mem` 1 GB, `wal_level` replica, `wal_compression` pglz, `shared_preload_libraries` pg_stat_statements. The file is `results/postgres-8gb.conf`. Autovacuum was off only while the initial COPY ran, then turned back on before any benchmark.

`pgbench` is shipped in the `postgresql-18` server package, so installing it on the load generator also created a local cluster. That cluster was stopped and disabled before the load. It was not part of the test.

## Data at 1M, before the benchmark

Load of posts 1 through 1,000,000, 100,000 users, 2,000,000 likes. Body lengths were 80 to 200 characters. Each loaded user had 10 posts.

| Step | Seconds |
|---|---|
| COPY users | 0.487 |
| COPY posts | 4.938 |
| COPY likes | 4.585 |
| users primary key | 0.246 |
| posts primary key | 1.728 |
| likes primary key | 3.194 |
| posts (user_id, created_at DESC) | 1.476 |
| VACUUM ANALYZE | 0.861 |

Sizes after that vacuum, before the benchmark writes:

| Relation | Heap | Indexes | Total |
|---|---|---|---|
| users | 6,160,384 | 2,260,992 | 8,462,336 |
| posts | 201,981,952 | 54,050,816 | 256,122,880 |
| likes | 104,726,528 | 63,062,016 | 167,845,888 |

Database size: 440,424,127 bytes (420 MB), which is 0.051 times 8 GiB of RAM.

The benchmark then inserted more rows. At 2026-10-05T14:20Z the database had 100,000 users, 1,060,473 posts, and 2,201,165 likes, and `pg_database_size` was 483,899,071 bytes (0.056 times 8 GiB). Post 1000000 had 60,262 likes. User 1 had 23,221 posts. Those two numbers are from counts after the runs, not from the initial load.

`pg_stat_statements` recorded 60,473 `INSERT` calls into posts and the table grew by 60,473 rows. It recorded 240,916 `INSERT` calls into likes and the table grew by 201,165 rows, so 39,751 like statements hit `ON CONFLICT DO NOTHING` and inserted nothing.

## Access pattern

A 10-second pgbench probe of `random_zipfian(..., 1.5)`, with post ids inverted so the mass sits on high ids, produced `results/zipfian-histogram.csv`. Of the post samples, 56,560 of about 56,650 fell in the top decile of ids. Of the user samples, 56,345 fell in the bottom decile. The skew is real, and it is sharp.

`EXPLAIN (ANALYZE, BUFFERS)` after the 20-second smoke, saved in `results/explain-1m.txt`, used `posts_user_created_idx` for the profile and `likes_pkey` for the like count. By then the smoke had already added likes, so the plan for post 1000000 counted 3,751 likes. That was not the later 60,262.

## The limit table

There is one attempted limit. It was not confirmed.

| Test | Posts | Database size | Size vs RAM | Total QPS | Read QPS | Write QPS | Users | p50 | p95 | p99 | Cache hit | What broke |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1M mixed | 1,000,000 loaded | 420 MB after load | 0.051× | not confirmed | | | | | | | 1.000000 on the search runs | CPU, see below |

The highest 2-minute open-loop run that passed was target rate 561 (`1m-mixed-search-04`). It is not the published limit, because the 5-minute run at the same rate failed.

| | 2-minute pass at 561 | 5-minute run at 561 |
|---|---|---|
| Total QPS | 558.253 | 555.798 |
| Read QPS | 504.114 | 500.157 |
| Write QPS | 54.139 | 55.641 |
| Users (QPS × 10) | 5,583 | |
| p50 / p95 / p99 (ms) | 3.324 / 80.002 / 187.055 | 6.886 / 1,123.895 / 2,761.911 |
| Error rate | 0 | 0 |
| CPU user+system | 79.2% | 89.8% |
| iowait | 0 | 0 |
| steal | 0.875% | 0.350% |
| Load generator CPU | 0.54% | 0.48% |
| Disk util / await | 2.1% / 0.34 ms | 2.0% / 0.38 ms |
| Cache hit | 1.000000 | 1.000000 |

Past that 2-minute pass, the next search rate was 576. It failed with p95 187.844 ms and p99 420.495 ms. CPU user+system was 87.6%, iowait was 0, disk util was 1.9%. What broke was CPU. The open-post query, which is `count(*)` of likes for one post id, had a mean execution time of 30.1 ms during the passing 2-minute run, 68.2 ms during the 5-minute run at 561, and 97.0 ms during the 5-minute run at 332. Over the same runs the profile query stayed at about 0.15 ms, and both inserts stayed under 0.2 ms. Those means are execution time inside Postgres, from `pg_stat_statements`, so they do not include pgbench queueing. The queueing shows up in the p95/p99 gap.

Closed-loop ceiling after the 10-minute warmup: 999.193 QPS, p95 231.866 ms, p99 299.448 ms, CPU 98.2%, iowait 0, steal 0.125%. That run failed the latency bar. The 20-second smoke before the hot key had grown did 6,486.179 QPS with p95 25.739 ms, but average steal was 9.5%, so that run is invalid and is not a result.

## The 5-minute runs

Every confirm below failed. p95 had to be under 100 ms. None of them were. There is no variance across three passing runs, because there were not three passing runs.

| Target | Total QPS | p50 | p95 | p99 | CPU | iowait | steal |
|---|---|---|---|---|---|---|---|
| 561 | 555.798 | 6.886 | 1,123.895 | 2,761.911 | 89.8% | 0 | 0.35% |
| 532 | 530.540 | 25.304 | 1,997.663 | 4,016.917 | 94.5% | 0 | 0.40% |
| 505 | 503.038 | 7.870 | 523.105 | 1,119.726 | 91.5% | 0 | 0.23% |
| 479 | 478.441 | 18.198 | 1,269.353 | 2,759.238 | 94.4% | 0 | 0.22% |
| 455 | 446.337 | 388.365 | 10,090.672 | 16,043.485 | 96.4% | 0 | 0.30% |
| 432 | 427.254 | 162.168 | 3,804.280 | 5,802.872 | 96.9% | 0 | 0.43% |
| 410 | 410.478 | 85.582 | 2,263.060 | 4,566.418 | 96.0% | 0 | 0.37% |
| 389 | 376.002 | 152.602 | 24,368.478 | 27,364.175 | 94.2% | 0 | 0.80% |
| 369 | 367.804 | 781.464 | 7,929.055 | 9,508.706 | 97.2% | 0 | 0.80% |
| 350 | 350.381 | 11.397 | 853.058 | 1,635.359 | 93.0% | 0 | 0.78% |
| 332 | 333.158 | 6.488 | 520.830 | 1,279.858 | 88.3% | 0 | 0.43% |

Lowering the rate did not produce a clean pass. The open-post mean got worse as the same post ids collected more likes. I do not know whether some rate below 315 would have passed three 5-minute runs. I did not measure that.

Checkpoints ran on the 15-minute timer and each spent 3 to 5 minutes writing. They are in the Postgres log. Disk util during the failing confirms stayed around 1–2% and iowait stayed at 0, so those checkpoints were not the latency.

## Thrown away

- `1m-mixed-smoke`: steal averaged 9.5%, above the 5% rule. Kept in `results/raw/` and in `runs.csv` as invalid. Not rerun, because the later runs on the same machine had steal under 1% and the smoke was only a wiring check.
- `1m-mixed-confirm-r315-1`: killed before pgbench finished. Not in `runs.csv`.

Load-generator CPU never approached 70%. The highest value in the csv is 3.5% on the invalid smoke.

## Cost and time

From 2026-10-05T12:41:29Z to 2026-10-05T14:20:26Z: database $0.1178, load generator $0.4123, total $0.53. They are still billing at $0.32143/hour until you tell me to destroy them. I have not destroyed anything.

## Demo runs

Both demos are closed loop, 50 clients, 10 minutes, `pgbench -P 10`. They are not limits. Each one started from a freshly loaded 1M posts, 100,000 users, and 2,000,000 likes. Steal stayed under 5% on the first try, so neither demo was rerun.

The interval tables are the `pgbench -P 10` progress lines. QPS is transactions per second in that 10-second window. Latency is the average latency pgbench printed for that window, in milliseconds. Every window reported 0 failed transactions.

### Demo A: hot key collapse

Original scripts. `random_zipfian(..., 1.5)`, and both reads compute `count(*)` from `likes`. The like write is `INSERT ... ON CONFLICT DO NOTHING` and does not maintain a counter. `posts.like_count` was not on the table.

At the start every post had 2 likes (1,000,000 posts tied). At the end one post was ahead: post 1000000 had 27,144 likes, post 999999 had 10,458, and post 999998 had 5,981.

Over the whole 10 minutes: 1,719.809 QPS (read 1,547.972, write 171.837), p50 6.949 ms, p95 155.464 ms, p99 280.073 ms. CPU user+system 97.6%, iowait 0, steal 2.108%, load-generator CPU 0.993%, cache hit 0.999999, disk util 5.27%, await 0.457 ms. `pg_stat_statements` for this run: the open-post `count(*)` averaged 47.416 ms across 463,535 calls. The profile query averaged 0.268 ms. The like insert averaged 0.519 ms (82,667 calls, 77,274 rows actually inserted). The new-post insert averaged 0.204 ms (20,454 calls, and the posts table grew by 20,454 rows).

QPS starts at 9,901 and ends at 836. Average latency starts at 4.942 ms and ends at 59.795 ms.

| Seconds | QPS | Avg latency (ms) | Stddev (ms) | Failed |
|---|---:|---:|---:|---:|
| 10.0 | 9901.2 | 4.942 | 5.152 | 0 |
| 20.0 | 6298.9 | 7.927 | 9.873 | 0 |
| 30.0 | 4619.6 | 10.819 | 13.279 | 0 |
| 40.0 | 4039.4 | 12.377 | 15.269 | 0 |
| 50.0 | 3734.9 | 13.380 | 16.370 | 0 |
| 60.0 | 3358.8 | 14.882 | 18.884 | 0 |
| 70.0 | 3008.1 | 16.598 | 21.183 | 0 |
| 80.0 | 2634.1 | 18.974 | 25.697 | 0 |
| 90.0 | 2409.7 | 20.735 | 28.364 | 0 |
| 100.0 | 2181.9 | 22.928 | 32.631 | 0 |
| 110.0 | 2141.5 | 23.341 | 32.700 | 0 |
| 120.0 | 1981.8 | 25.214 | 36.190 | 0 |
| 130.0 | 1881.1 | 26.586 | 37.932 | 0 |
| 140.0 | 1933.3 | 25.809 | 37.367 | 0 |
| 150.0 | 1996.6 | 25.051 | 36.044 | 0 |
| 160.0 | 1974.9 | 25.308 | 35.896 | 0 |
| 170.0 | 1631.3 | 30.624 | 44.950 | 0 |
| 180.0 | 1796.1 | 27.869 | 40.386 | 0 |
| 190.0 | 1587.9 | 31.462 | 46.587 | 0 |
| 200.0 | 1472.8 | 33.915 | 51.258 | 0 |
| 210.0 | 1427.4 | 35.004 | 52.057 | 0 |
| 220.0 | 1615.1 | 30.992 | 46.991 | 0 |
| 230.0 | 1559.0 | 32.071 | 47.986 | 0 |
| 240.0 | 1527.6 | 32.710 | 49.498 | 0 |
| 250.0 | 1549.3 | 32.304 | 47.671 | 0 |
| 260.0 | 1550.6 | 32.208 | 48.090 | 0 |
| 270.0 | 1437.7 | 34.798 | 52.699 | 0 |
| 280.0 | 1238.4 | 40.333 | 62.769 | 0 |
| 290.0 | 1194.8 | 41.834 | 66.480 | 0 |
| 300.0 | 1187.5 | 42.039 | 65.050 | 0 |
| 310.0 | 1224.1 | 40.847 | 64.570 | 0 |
| 320.0 | 1162.7 | 42.904 | 69.423 | 0 |
| 330.0 | 1074.0 | 46.707 | 74.136 | 0 |
| 340.0 | 1063.6 | 46.766 | 75.817 | 0 |
| 350.0 | 1102.1 | 45.536 | 72.830 | 0 |
| 360.0 | 1083.9 | 46.126 | 71.399 | 0 |
| 370.0 | 1077.8 | 46.296 | 73.887 | 0 |
| 380.0 | 963.0 | 51.817 | 84.159 | 0 |
| 390.0 | 901.0 | 55.629 | 89.814 | 0 |
| 400.0 | 969.5 | 51.531 | 81.959 | 0 |
| 410.0 | 988.0 | 50.598 | 82.167 | 0 |
| 420.0 | 1002.5 | 49.787 | 80.521 | 0 |
| 430.0 | 969.0 | 51.659 | 82.951 | 0 |
| 440.0 | 934.7 | 53.523 | 85.097 | 0 |
| 450.0 | 906.2 | 55.142 | 88.543 | 0 |
| 460.0 | 935.6 | 53.239 | 84.936 | 0 |
| 470.0 | 929.0 | 54.039 | 88.880 | 0 |
| 480.0 | 852.7 | 58.312 | 94.618 | 0 |
| 490.0 | 836.6 | 60.067 | 99.853 | 0 |
| 500.0 | 888.6 | 56.096 | 93.213 | 0 |
| 510.0 | 855.2 | 58.351 | 92.615 | 0 |
| 520.0 | 868.5 | 57.829 | 93.683 | 0 |
| 530.0 | 876.5 | 56.761 | 91.900 | 0 |
| 540.0 | 808.8 | 62.016 | 96.699 | 0 |
| 550.0 | 885.4 | 56.487 | 92.505 | 0 |
| 560.0 | 829.5 | 59.997 | 98.597 | 0 |
| 570.0 | 820.2 | 61.354 | 100.732 | 0 |
| 580.0 | 815.4 | 61.180 | 101.088 | 0 |
| 590.0 | 871.3 | 57.382 | 93.548 | 0 |
| 600.0 | 835.5 | 59.795 | 95.911 | 0 |

### Schema change

`posts.like_count` is `integer NOT NULL DEFAULT 0`. After the fresh copy for demo B, one `UPDATE` set it from `likes`: 1,000,000 rows in 7.414 seconds. The check after that update was min 2, max 2, sum 2,000,000, and zero posts left at any other value.

The like script is one statement. It inserts the like, and the `UPDATE` of `posts.like_count` reads only the rows `INSERT ... ON CONFLICT DO NOTHING RETURNING` actually produced:

```sql
WITH inserted AS (
    INSERT INTO likes (post_id, user_id, created_at)
    VALUES (:pid, :uid, now())
    ON CONFLICT DO NOTHING
    RETURNING post_id
)
UPDATE posts AS p
SET like_count = p.like_count + 1
FROM inserted AS i
WHERE p.id = i.post_id;
```

Both reads select `posts.like_count` and do not touch `likes`. A rolled-back execution of this statement left post 1000000 at like_count 2 with no like row for the test user.

### Demo B: the same zipfian load against the counter

Same picks as demo A: zipfian 1.5, uniform user on the like. Fresh 1M load with the column. Database size after the load and vacuum was 655,849,151 bytes, 0.076 times 8 GiB.

At the start the maximum `like_count` was 2, shared by all 1,000,000 posts, and the three highest ids each had `likes_rows` 2. At the end post 1000000 had `like_count` 98,268 and 98,268 likes rows. Post 999999 had 75,805 on both. Post 999998 had 53,789 on both.

Over the whole 10 minutes: 21,906.308 QPS (read 19,716.116, write 2,190.192), p50 1.269 ms, p95 6.229 ms, p99 14.130 ms. CPU user+system 91.0%, iowait 0.692%, steal 2.725%, load-generator CPU 14.386%, cache hit 0.999968, disk util 27.58%, await 0.370 ms. Open-post mean execution time was 0.032 ms (5,913,478 calls). Profile was 0.098 ms. The like statement was 4.774 ms (1,051,539 calls). New post was 0.134 ms (262,378 calls, matching the growth in `n_tup_ins`). `n_tup_upd` on posts rose by 627,931 and `n_tup_ins` on likes rose by 627,931, so the counter moved once per inserted like. The other 423,608 like statements hit `ON CONFLICT DO NOTHING` and did not increment the counter.

Lock samples were taken every 5 seconds (116 samples). 72 samples had at least one session in a `Lock` wait, and 70 had a session blocked by another. The most sessions waiting on a lock in one sample was 35. The most ungranted locks on `posts` in one sample was 19. Ungranted locks on `likes` were 0 in every sample. The lock wait events seen were `transactionid` (up to 35 sessions in a sample) and `tuple` (up to 10). Across the run, 81 ungranted `posts` lock rows were recorded. Those waits sit on the hot post row: the like statement is the only one that updates `posts`, and the hottest row picked up 98,268 likes.

QPS stayed between 14,931.2 and 27,354.2. Average latency stayed between 1.825 ms and 3.342 ms. The lowest QPS window is the one at 380 seconds.

| Seconds | QPS | Avg latency (ms) | Stddev (ms) | Failed |
|---|---:|---:|---:|---:|
| 10.0 | 18258.4 | 2.669 | 15.225 | 0 |
| 20.0 | 18166.7 | 2.747 | 16.418 | 0 |
| 30.0 | 16787.1 | 2.974 | 17.907 | 0 |
| 40.0 | 18778.5 | 2.657 | 16.169 | 0 |
| 50.0 | 21071.6 | 2.371 | 14.227 | 0 |
| 60.0 | 21954.4 | 2.277 | 13.444 | 0 |
| 70.0 | 22184.2 | 2.251 | 13.311 | 0 |
| 80.0 | 21734.1 | 2.297 | 13.385 | 0 |
| 90.0 | 23562.6 | 2.119 | 12.131 | 0 |
| 100.0 | 22583.7 | 2.210 | 12.356 | 0 |
| 110.0 | 23735.9 | 2.103 | 11.816 | 0 |
| 120.0 | 24627.3 | 2.027 | 10.557 | 0 |
| 130.0 | 24497.9 | 2.040 | 10.083 | 0 |
| 140.0 | 23156.8 | 2.155 | 8.656 | 0 |
| 150.0 | 23939.0 | 2.086 | 7.906 | 0 |
| 160.0 | 24999.0 | 1.997 | 5.785 | 0 |
| 170.0 | 24729.1 | 2.017 | 4.786 | 0 |
| 180.0 | 27354.2 | 1.825 | 4.184 | 0 |
| 190.0 | 25938.9 | 1.922 | 3.904 | 0 |
| 200.0 | 25483.3 | 1.959 | 3.087 | 0 |
| 210.0 | 26579.8 | 1.877 | 2.832 | 0 |
| 220.0 | 26298.8 | 1.897 | 2.656 | 0 |
| 230.0 | 25445.1 | 1.959 | 2.731 | 0 |
| 240.0 | 27306.2 | 1.827 | 2.469 | 0 |
| 250.0 | 27274.9 | 1.829 | 2.346 | 0 |
| 260.0 | 26166.0 | 1.906 | 2.368 | 0 |
| 270.0 | 27090.4 | 1.841 | 2.336 | 0 |
| 280.0 | 26218.1 | 1.903 | 2.271 | 0 |
| 290.0 | 21374.5 | 2.332 | 2.940 | 0 |
| 300.0 | 17180.9 | 2.906 | 3.902 | 0 |
| 310.0 | 18794.5 | 2.654 | 2.770 | 0 |
| 320.0 | 16201.2 | 3.080 | 4.254 | 0 |
| 330.0 | 19131.3 | 2.608 | 2.731 | 0 |
| 340.0 | 16632.7 | 3.000 | 3.045 | 0 |
| 350.0 | 18366.5 | 2.717 | 3.005 | 0 |
| 360.0 | 20924.5 | 2.385 | 2.560 | 0 |
| 370.0 | 20135.3 | 2.478 | 2.547 | 0 |
| 380.0 | 14931.2 | 3.342 | 3.308 | 0 |
| 390.0 | 20275.4 | 2.461 | 2.588 | 0 |
| 400.0 | 22930.2 | 2.176 | 2.364 | 0 |
| 410.0 | 21382.2 | 2.333 | 2.526 | 0 |
| 420.0 | 23805.1 | 2.096 | 2.294 | 0 |
| 430.0 | 23286.9 | 2.142 | 2.304 | 0 |
| 440.0 | 19970.7 | 2.499 | 2.792 | 0 |
| 450.0 | 23210.9 | 2.149 | 2.237 | 0 |
| 460.0 | 22651.7 | 2.202 | 2.343 | 0 |
| 470.0 | 19677.3 | 2.536 | 2.485 | 0 |
| 480.0 | 22929.4 | 2.176 | 2.231 | 0 |
| 490.0 | 23745.7 | 2.101 | 2.175 | 0 |
| 500.0 | 21849.8 | 2.283 | 2.352 | 0 |
| 510.0 | 21651.4 | 2.304 | 2.319 | 0 |
| 520.0 | 21003.3 | 2.376 | 2.390 | 0 |
| 530.0 | 20025.2 | 2.492 | 2.578 | 0 |
| 540.0 | 20794.0 | 2.399 | 2.372 | 0 |
| 550.0 | 19061.3 | 2.618 | 2.529 | 0 |
| 560.0 | 20438.3 | 2.440 | 2.459 | 0 |
| 570.0 | 19358.8 | 2.579 | 2.489 | 0 |
| 580.0 | 18650.9 | 2.676 | 2.589 | 0 |
| 590.0 | 17630.6 | 2.830 | 2.795 | 0 |
| 600.0 | 20252.8 | 2.464 | 3.769 | 0 |

## 80/20 access pattern

Every limit run from here on drops zipfian. For posts, 80% of operations draw uniformly from the newest 20% of the loaded ids, which is 800001 through 1000000, and 20% draw uniformly from 1 through 1000000. The same split applies to users, on ids 80001 through 100000. It is used for the profile user, the open-post id, the new-post user, and both ids on the like. The bounds stay at the loaded 1,000,000 posts. Inserts during a run take the next identity value, above 1000000, and the scripts do not pick those new ids.

The arithmetic of that rule puts 84% of draws inside the newest 20%, because the uniform branch also lands there a fifth of the time. A 10-second probe wrote 96,006 samples. 80,485 post ids (83.8%) and 80,764 user ids (84.1%) were in the newest 20%. The decile counts:

| Decile | Post samples | User samples |
|---|---:|---:|
| 1 | 1,955 | 1,871 |
| 2 | 1,921 | 1,955 |
| 3 | 2,010 | 1,867 |
| 4 | 1,961 | 1,868 |
| 5 | 1,880 | 1,993 |
| 6 | 1,899 | 1,867 |
| 7 | 1,971 | 1,945 |
| 8 | 1,924 | 1,876 |
| 9 | 40,196 | 40,708 |
| 10 | 40,289 | 40,056 |

Deciles 9 and 10 are the newest 20%. The file is `results/access-8020-histogram.csv`.

`EXPLAIN (ANALYZE, BUFFERS)` on the fresh load, saved in `results/explain-1m-8020.txt`: the profile for user 80001 is an index scan on `posts_user_created_idx`, 10 rows, execution 0.193 ms, shared hits only. The open of post 1000000 is an index scan on `posts_pkey`, execution 0.050 ms, shared hits only.

## Confirmed 1M mixed limit

Fresh 1M load again, with `like_count` and the 80/20 scripts. Database size after that load was 655,873,727 bytes, 0.076 times 8 GiB. The method was the original one: 10-minute closed-loop warmup, one 2-minute closed-loop ceiling, binary search with 2-minute open-loop runs until the bracket was about 5% wide, then three 5-minute open-loop runs. Pass is still p95 under 100 ms, p99 under 500 ms, and failed or skipped under 1%.

The warmup passed: 23702.612 QPS, p95 6.869 ms, p99 10.964 ms, CPU 94.067%, iowait 0.283%, steal 1.692%, load-generator CPU 13.087%. The ceiling passed too: 22,271.910 QPS, read 20,038.949, write 2,232.961, p50 1.502 ms, p95 7.228 ms, p99 11.239 ms, CPU 93.0%, iowait 0.167%, steal 1.625%, cache hit 1.000000, load-generator CPU 13.746%, disk util 21.76%, await 0.35 ms. The ceiling is closed loop. It is not the published limit.

The search bracket closed between a pass at offered rate 13,919 (`1m-8020-search-03`: measured 13,919.087 QPS, p95 3.480 ms, p99 27.038 ms, CPU 72.5%, steal 0.833%) and a fail at 14,615. That gap is 4.8% of 14,615.

All three 5-minute runs at 13,919 passed. That offered rate is the published limit.

| Run | Total QPS | Read QPS | Write QPS | Users | p50 | p95 | p99 | CPU | iowait | steal |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 13,915.003 | 12,525.234 | 1,389.768 | 139,150 | 0.825 | 5.971 | 278.075 | 76.0% | 1.917% | 1.267% |
| 2 | 13,917.266 | 12,526.723 | 1,390.543 | 139,173 | 0.835 | 6.284 | 47.286 | 75.5% | 1.983% | 1.517% |
| 3 | 13,916.086 | 12,522.119 | 1,393.967 | 139,161 | 0.750 | 3.358 | 17.053 | 74.6% | 1.933% | 0.750% |

Users are measured total QPS times 10. Error rate was 0 on all three. Cache hit was 1.000000. Load-generator CPU was 10.552%, 10.404%, and 10.484%. Disk util was 31.72%, 31.93%, and 30.69%, with await 0.226 ms, 0.219 ms, and 0.191 ms. Open-post mean execution time was 0.033 ms, 0.033 ms, and 0.032 ms. Profile stayed at 0.106 ms, 0.106 ms, and 0.102 ms. The like statement was 0.165 ms, 0.116 ms, and 0.155 ms. The hottest `like_count` moved from 28 to 34 across the three runs, and on each sampled post `like_count` matched the number of likes rows.

The p99 spread, 17.053 ms to 278.075 ms, is the opening of the first confirm. In that run the first 10 seconds of the log had p99 814.616 ms, and the other 290 seconds had p99 15.819 ms. The whole-run p99 of 278.075 ms is still under 500 ms, so the run passed.

The first failing search step above 13,919 was the 2-minute run at offered rate 14,615 (`1m-8020-search-05`). Measured QPS 14,605.147, p95 11.728 ms, p99 921.925 ms, CPU 75.5%, iowait 1.875%, steal 0.875%, disk util 31.32%, await 0.21 ms, cache hit 1.000000, load-generator CPU 11.682%, errors 0. The wait sampler recorded no `Lock` waits. Open-post mean execution time was 0.032 ms and the like statement was 0.118 ms. The p99 came from the first 10 seconds of the log, where p99 was 1,259.219 ms. The other 110 seconds had p95 3.667 ms and p99 13.695 ms. Higher failing steps were worse on the same latency bar: 15,311 had p95 800.382 ms and p99 1,558.853 ms, and 16,703 had p95 413.747 ms and p99 926.745 ms. CPU on those runs was 79.6% and 83.5%. The queries themselves stayed under 0.2 ms.

One search attempt was thrown out. `1m-8020-search-04` at rate 15,311 had average steal 6.250%, above 5%. It is in `results/raw/` and `runs.csv` as invalid. The rerun at the same rate had steal 2.625% and failed the latency bar, so that rate is a real failure.

After the warmup, the search, and the three confirms, the database held 100,000 users, 1,795,802 posts, and 5,179,455 likes. `pg_database_size` was 1,034,507,967 bytes, 0.120 times 8 GiB. WAL was 1,358,954,496 bytes. Database plus WAL is far under 120 GB.

## Ramp exclusion, same 1M workload

Open-loop runs now last 30 seconds longer than the scored window. A 2-minute search is 150 seconds of pgbench, and a 5-minute confirm is 330 seconds. Percentiles and the error rate use only log lines at or after 30 seconds from the first timestamp. The raw logs still contain the excluded seconds. Closed-loop warmup and the closed-loop ceiling are unchanged.

The pass rule is unchanged: p95 under 100 ms, p99 under 500 ms, failed or skipped under 1%. The latency added into that percentile is still pgbench's logged `time` plus `schedule_lag`. In pgbench 18 the logged `time` is already measured from the scheduled start, so it already contains `schedule_lag`. When the scheduler is behind, the percentile counts that delay twice. The tables below give both numbers where that changes the reading: the scored p99 is the pass/fail number, and "pgbench p99" is the logged `time` alone.

### What the startup spike is

Connections are made before pgbench starts the clock, so they are not in the log. On the new 13,919 run the 50 first transactions inside the excluded 30 seconds had execution p50 22.2 ms and lag p99 33.5 ms. They are not the spike.

The spike is schedule lag on the transactions behind those. In the first 10 seconds, p99 of `time + schedule_lag` was 804 ms, lag p99 was 400 ms, and execution p99 was 14 ms. By 30–40 seconds the mean of `time + schedule_lag` was 1.9 ms. The same split on the earlier 14,615 run (`1m-8020-search-05`) put the excluded 30 seconds at p99 1,160 ms, lag p99 578 ms, execution p99 9.4 ms, and the rest of that run at p99 11.4 ms.

pgbench 18 starts each thread's Poisson schedule at `bench_start`. The first use of a prepared statement calls synchronous `PQprepare` inside the timed transaction, on the thread that also feeds the other clients. The schedule keeps advancing while that call runs, so the following transactions start late. Actual execution stays around 10 ms, which matches `pg_stat_statements`. The backlog has drained by about 30 seconds when the offered rate is sustainable. That is the startup spike: the scheduler falling behind, not connection setup and not a slow plan.

Later multi-second lag, in the confirms below, is a separate event. Execution p99 stays under 16 ms on those runs too.

### 13,919 beside the new result

| | Previous 80/20 method | Ramp exclusion |
|---|---:|---:|
| Confirmed offered QPS | 13,919 | none |
| Confirmed users (measured QPS × 10) | 139,150 to 139,173 | none |
| Highest 2-minute pass, offered QPS | 13,919 | 18,943 |
| 5-minute outcome | three passes at 13,919 | no rate produced three passes |

The fresh load for this retest was posts 1–1,000,000, 100,000 users, 2,000,000 likes, with `like_count` set to 2. Database size after the load was 655,972,031 bytes, 0.076 times 8 GiB. WAL was 1,358,954,496 bytes. A 10-second 80/20 probe drew 86,081 ids. 72,438 post ids (84.2%) were in 800,001–1,000,000, and 72,171 user ids (83.8%) were in 80,001–100,000. Those bounds are the newest 20% of the loaded ids. The histogram is `results/access-8020-histogram-1m-ramp.csv`.

The 10-minute closed-loop warmup passed: 25,175.281 QPS, read 22,659.091, write 2,516.190, p95 6.557 ms, p99 10.552 ms, CPU 95.5%, iowait 0.250%, steal 1.008%, cache hit 0.999918, load-generator CPU 15.024%, disk util 22.29%, await 0.353 ms. The 2-minute closed-loop ceiling passed: 23,968.185 QPS, read 21,574.313, write 2,393.872, p50 1.389 ms, p95 6.762 ms, p99 10.577 ms, CPU 94.5%, iowait 0.125%, steal 0.708%, cache hit 1.000000, load-generator CPU 12.540%. The ceiling is not a published limit.

The search started at offered 13,919 and went up. That run passed. The bracket then closed between a pass at 18,943 and a fail at 19,571, which is 3.2% of 19,571.

| Offered | Run | Result | Measured QPS | p95 | p99 | Ramp p99 | Ramp lag p99 | Ramp exec p99 | Steal |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 13,919 | search-01 | pass | 13,879.600 | 76.387 | 358.983 | 723.577 | 359.847 | 13.292 | 4.167% |
| 18,943 | search-02 | invalid | 16,609.925 | 39,478.158 | 42,367.396 | 2,191.816 | 1,094.268 | 11.517 | 5.367% |
| 18,943 | search-02 rerun | invalid | 17,968.875 | 26,907.272 | 29,548.160 | 10,715.589 | 5,356.361 | 15.525 | 5.900% |
| 18,943 | search-02 rerun 3 | pass | 18,901.258 | 23.432 | 323.659 | 821.596 | 409.597 | 8.412 | 0.367% |
| 21,455 | search-03 | fail | 20,808.375 | 8,371.646 | 9,375.792 | 1,629.233 | 813.532 | 10.806 | 1.367% |
| 20,199 | search-04 | fail | 19,813.250 | 11,262.035 | 12,697.765 | 9,053.220 | 4,525.427 | 16.289 | 1.600% |
| 19,571 | search-05 | fail | 18,485.367 | 25,392.197 | 29,007.906 | 7,993.587 | 3,995.440 | 14.030 | 3.967% |

Error rate was 0 on every row, including the excluded 30 seconds. Cache hit was 1.000000 on the search rows. The valid 18,943 pass measured read 17,017.192 QPS and write 1,884.067 QPS, CPU 87.0%, iowait 0.967%, load-generator CPU 13.912%, disk util 27.71%, await 0.209 ms. The two 18,943 runs with steal above 5% are invalid and are not the pass/fail for that rate.

### Why the 5-minute runs did not confirm

The 2-minute candidate was offered 18,943. Five step-downs followed. None of the six rates produced three passing 5-minute runs. The loop stopped at 14,656, as specified. 10M, 100M, and the write-only test were not started.

| Offered | Run | Result | Measured QPS | p50 | p95 | p99 | pgbench p99 | Exec p99 | Ramp p99 | CPU | iowait | Steal |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 18,943 | 1 | fail | 18,623.533 | 13,546.649 | 24,277.618 | 25,876.899 | | 12.113 | 1,699.713 | 88.7% | 0.818% | 3.424% |
| 17,995 | 1 | fail | 18,000.450 | 1.078 | 108.736 | 500.594 | 251.5 | 9.0 | 2,434.641 | 87.3% | 1.000% | 1.197% |
| 17,095 | 1 | fail | 17,101.850 | 2.111 | 535.873 | 1,671.888 | 837.4 | 10.6 | 2,499.010 | 85.9% | 1.000% | 2.939% |
| 16,240 | 1 | pass | 16,244.460 | 0.730 | 4.780 | 109.915 | 56.5 | 5.8 | 223.119 | 79.3% | 1.667% | 0.470% |
| 16,240 | 2 | fail | 15,986.173 | 1.039 | 80.308 | 2,850.587 | 1,427.1 | 9.5 | 115.546 | 82.7% | 1.167% | 1.727% |
| 15,428 | 1 | fail | 15,406.147 | 2.301 | 1,650.372 | 5,807.486 | 2,905.3 | 13.2 | 504.379 | 82.7% | 1.258% | 4.182% |
| 14,656 | 1 | fail | 14,658.037 | 1.263 | 5,060.260 | 7,651.011 | 3,827.1 | 11.6 | 251.488 | 80.6% | 1.606% | 3.076% |

pgbench p99 is blank for the 18,943 confirm because that run's 10-second progress latency already sat between 4 and 8 seconds, which is over 500 ms on its own. Read QPS on the single passing confirm was 14,623.493 and write QPS was 1,620.967. Load-generator CPU on these confirms was 10.6% to 13.0%. Cache hit was 1.000000. Error rate was 0. Disk util was 25% to 34%, and await stayed under 0.43 ms.

The queries did not get slower. Across these confirms, `pg_stat_statements` mean time stayed at 0.029–0.037 ms for the open post, 0.104–0.136 ms for the profile, and 0.131–0.222 ms for the like statement. The hottest `like_count` moved from 29 to 52, and on each sampled post it matched the number of likes rows. That is not the earlier `count(*)` collapse, where one post reached tens of thousands of likes and the open-post mean climbed from 30 ms to 97 ms.

What moved is schedule lag, in bursts, long after the excluded 30 seconds.

- At 18,943 the progress latency was 399 ms at 30 seconds, 4,588 ms at 120 seconds, and 6,236–8,676 ms from there to the end. Throughput dipped to about 17,200 tps and later climbed back over the offered rate while the latency stayed in seconds. The backlog did not drain.
- At 17,995 the run recovered. Progress latency was 1–3 ms from 150 seconds on. pgbench's own p99 was 251.5 ms and its p95 was 55.8 ms, both inside the limits. The pass rule adds `schedule_lag` a second time, and that is what put p95 at 108.736 ms and p99 at 500.594 ms.
- At 17,095, pgbench's own p95 was 269 ms and its p99 was 837 ms. The run had a 385 ms interval at 60 seconds and then sat near 2–20 ms.
- At 16,240 the first confirm stayed near 1 ms except for one 32 ms interval, and it passed. The repeat stayed near 2 ms for 320 seconds. The last 10 seconds fell to 8,470 tps with progress latency 2,026 ms. pgbench's own p99 on that repeat was 1,427 ms.
- At 15,428, progress latency was 16–431 ms for most of the run, then 1–7 ms in the last minute. pgbench's own p95 was 827 ms.
- At 14,656, progress latency was 1–8 ms until 240 seconds, then one interval averaged 3,306 ms and the run recovered to about 2 ms. pgbench's own p99 was 3,827 ms. This offered rate is above the previously confirmed 13,919, and one 10-second stall was enough to fail the 5-minute percentile.

CPU steal lines up with those outcomes. The only 5-minute pass averaged 0.47% steal, with one 5-second sample above 5%. The failures averaged 1.2% to 4.2% steal. The 14,656 run had 12 samples above 5%, and its peak 5-second sample was 10%. None of these confirms averaged over 5%, so they stay in the result. iowait stayed near 1%, and database CPU was 79% to 89%.

A timed checkpoint was in progress during every confirm. Each one wrote for 810 seconds (`checkpoint_timeout` 15 min, `checkpoint_completion_target` 0.9). The passing 16,240 run also overlapped a checkpoint for its first three minutes and stayed near 1 ms, so the checkpoint write by itself does not account for a multi-second stall. The stalls show up as the load generator missing its schedule while query execution stays near 10 ms.

After these runs the database held 100,000 users, 2,502,907 posts, and 8,010,508 likes. `pg_database_size` was 1,439,446,719 bytes, 0.168 times 8 GiB. WAL was 1,509,949,440 bytes. Database plus WAL is under 120 GB.

## Cost so far

From 2026-10-05T12:41:29Z to 2026-10-05T18:24:40Z is 5.7198 hours. Database $0.4086, load generator $1.4300, total $1.84. Both droplets are still up, still billing at $0.32143/hour. Nothing has been destroyed.

## Not loaded

10M, 100M, and the write-only run were not started, because the 1M 5-minute confirms failed after five step-downs. The replica and the resize have not been started.
