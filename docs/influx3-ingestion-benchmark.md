# InfluxDB 3 ingestion tuning

This benchmark selected the default ingestion settings for InfluxDB 3 Core.
It was run on Core 3.11.1 for macOS ARM64 (binary SHA-256
`c69df9525adf916ebfafd4d9713fdff3ddf8999adfeeb7d938679708e238f801`).
Each result used a fresh managed database workspace. Rates below are accepted
line-protocol rows per second; every row contains ten field metrics.

## Datasets

| Mode | Dataset | Rows | Metrics | Bytes | SHA-256 |
| --- | --- | ---: | ---: | ---: | --- |
| Sync | `cpu-only-s100-003a91156529` | 864,000 | 8,640,000 | 297,699,448 | `d287426a327c38a84ba495b533d8a7512a6a84bd271d5b6d85df6b8ad0f0d732` |
| No-sync | `cpu-only-s1000-45c02116670b` | 8,640,000 | 86,400,000 | 2,989,015,165 | `33f08ef6aedf56b39a64598142f12c39779e7ececcc0df6e0471879c947448cd` |

Both datasets cover one day at a 10-second interval with seed 123. The larger
no-sync dataset provides enough requests to keep up to 16 workers active.
Because the modes use different dataset sizes, their rates are not a controlled
durability overhead comparison.

## Durable sync results

The batch-size screen used two workers:

| Batch rows | Rows/s |
| ---: | ---: |
| 3,000 | 6,029 |
| 10,000 | 19,819 |
| 20,000 | 39,736 |
| 25,000 | 49,019 |
| 30,000 | 59,735 |
| 100,000 | Rejected: HTTP 413, 10 MiB request limit |

Worker screening showed near-linear acknowledgement scaling at smaller batches:

| Batch rows | 1 worker | 2 workers | 4 workers | 8 workers | 16 workers |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 3,000 | 3,000 | 6,029 | 12,035 | 24,343 | 49,092 |
| 10,000 | 9,997 | 19,819 | 39,697 | 80,013 | 152,808 |

Adaptive trials reached 193,422 rows/s at 25,000 rows and 8 workers, and
192,018 rows/s at 30,000 rows and 8 workers. The finalists were repeated three
times:

| Batch rows | Workers | Run 1 | Run 2 | Run 3 | Median rows/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 25,000 | 16 | 287,157 | 321,880 | 323,468 | 321,880 |
| 20,000 | 16 | 250,594 | 300,870 | 298,074 | 298,074 |

The durable default is therefore 25,000 rows and 16 workers. This is about 53
times the 6,029 rows/s measured for the previous 3,000-row, two-worker default
on the same dataset.

To remove the small dataset's request-count limitation, 25,000 and 30,000 rows
were also compared at 16 workers on the 8.64-million-row dataset:

| Batch rows | Workers | Run 1 | Run 2 | Run 3 | Median rows/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 25,000 | 16 | 371,399 | 349,529 | 363,538 | 363,538 |
| 30,000 | 16 | 297,017 | 327,809 | 335,675 | 327,809 |

The larger dataset confirms 25,000 rows: its median was 10.9% higher than
30,000 rows, with every run accepting all 8.64 million rows.

## No-sync results

At two workers, batch size had little effect: 3,000, 10,000, 20,000, 25,000,
and 30,000 rows produced 490,346, 510,710, 497,183, 494,492, and 501,308
rows/s respectively. The concurrency screen was:

| Batch rows | 1 worker | 2 workers | 4 workers | 8 workers | 16 workers |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 3,000 | 305,670 | 490,346 | 744,140 | 1,193,492 | 1,193,433 |
| 10,000 | 311,153 | 510,710 | 837,991 | 1,093,859 | 1,199,947 |
| 30,000 | 303,832 | 501,308 | 767,768 | 991,002 | 1,005,975 |

The finalists were repeated three times:

| Batch rows | Workers | Run 1 | Run 2 | Run 3 | Median rows/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 16 | 1,199,947 | 1,148,305 | 1,202,085 | 1,199,947 |
| 3,000 | 8 | 1,193,492 | 1,187,417 | 1,034,609 | 1,187,417 |

The medians differ by about 1.1%, so the selection rule prefers the lower-cost
3,000-row, eight-worker configuration. No-sync remains opt-in because it does
not wait for durable WAL acknowledgement:

```bash
python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py load \
  --database-id core-311 --batch-size 3000 --load-workers 8 --no-sync
```

InfluxDB Core limits write request bodies to 10 MiB for this setup. The
100,000-row request exceeded that limit, so 200,000- and 300,000-row requests
were not attempted. The managed runner waits up to 60 seconds for shutdown so
accepted no-sync writes can flush before the server is stopped.

## Memory usage

Memory was measured on the same 16 GiB, 10-core macOS ARM64 host using the
8.64-million-row dataset. Resident set size (RSS) for the InfluxDB server and
TSBS loader was sampled every 50 ms. Server peak includes the graceful
post-load flush; combined peak is the maximum simultaneously sampled server
plus loader RSS, or the later server-only peak if higher. Every trial ingested
exactly 8.64 million rows and 86.4 million metrics and stopped cleanly.

These are single-run directional measurements, not stable medians. The host
was already under severe memory pressure (about 34 GiB of swap in use), which
can affect both RSS and throughput. Compare relative trends within this table,
and rerun from a clean boot for capacity planning.

### No-sync memory

| Batch rows | Workers | Rows/s | Server peak (MiB) | Loader peak (MiB) | Combined peak (MiB) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 3,000 | 2 | 470,934 | 1,599 | 138 | 1,599 |
| 10,000 | 2 | 468,638 | 1,630 | 136 | 1,750 |
| 25,000 | 2 | 475,043 | 1,672 | 411 | 2,060 |
| 30,000 | 2 | 478,263 | 1,671 | 422 | 2,034 |
| 3,000 | 8 | 970,078 | 3,682 | 286 | 3,682 |
| 10,000 | 8 | 1,113,098 | 4,785 | 355 | 4,785 |
| 25,000 | 8 | 1,044,630 | 4,541 | 1,152 | 5,281 |
| 30,000 | 8 | 973,623 | 4,552 | 1,194 | 4,663 |
| 3,000 | 16 | 1,074,099 | 4,474 | 481 | 4,955 |
| 10,000 | 16 | 1,059,085 | 4,342 | 574 | 4,376 |
| 25,000 | 16 | 936,068 | 4,650 | 1,457 | 4,650 |
| 30,000 | 16 | 923,904 | 5,149 | 1,626 | 5,149 |

At two workers, larger batches increased loader RSS without materially
improving throughput. At eight or 16 workers, 25,000- and 30,000-row batches
used more than 1 GiB of loader RSS and were slower than smaller batches. The
3,000-row, eight-worker recommendation remains a good memory/performance
balance; 10,000 rows and eight workers was 15% faster in this run but increased
server peak RSS by about 1.1 GiB.

### Sync memory

Only previously fast sync configurations were measured to avoid long
acknowledgement-bound trials:

| Batch rows | Workers | Rows/s | Server peak (MiB) | Loader peak (MiB) | Combined peak (MiB) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 16 | 160,117 | 971 | 613 | 1,553 |
| 25,000 | 8 | 197,443 | 895 | 1,063 | 1,904 |
| 25,000 | 16 | 363,351 | 1,165 | 1,467 | 2,475 |
| 30,000 | 8 | 233,442 | 1,144 | 1,218 | 2,324 |
| 30,000 | 16 | 342,708 | 1,414 | 1,772 | 3,100 |

The selected durable default, 25,000 rows and 16 workers, was the fastest sync
cell and used about 2.42 GiB combined peak RSS. Increasing the batch to 30,000
rows reduced throughput by 5.7% while adding about 625 MiB of combined peak
RSS, reinforcing the 25,000-row choice.

For matching cells, sync used substantially less server and combined peak RSS
than no-sync because durable acknowledgements bound in-flight writes:

| Batch rows | Workers | Sync combined (MiB) | No-sync combined (MiB) | Sync reduction |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 16 | 1,553 | 4,376 | 65% |
| 25,000 | 8 | 1,904 | 5,281 | 64% |
| 25,000 | 16 | 2,475 | 4,650 | 47% |
| 30,000 | 8 | 2,324 | 4,663 | 50% |
| 30,000 | 16 | 3,100 | 5,149 | 40% |
