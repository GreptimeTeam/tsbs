---
name: benchmark-influxdb3
description: Run repeatable InfluxDB 3 Core and Enterprise TSBS benchmarks on file or S3 storage. Use for smoke or performance tests, storage or edition comparisons, ingestion tests, query measurements, managed workspaces, or external endpoints.
---

# Benchmark InfluxDB 3

Use `scripts/benchmark.py` for execution and structured result parsing. Read
`references/workload.md` before choosing workloads, editions, durability flags,
or database modes. Use `$setup-influxdb3` to install and prepare a managed
database workspace and `$generate-tsbs-data` for standalone dataset operations.
Builds automatically use `$setup-tsbs-environment` to reuse Go 1.21+ or prepare
the verified repository-local fallback.

## Collect inputs

1. Select `all`, `generate`, `load`, `query`, or `summarize`.
2. For load/query stages, select exactly one target:
   - managed: a prepared `--database-id`;
   - external: repeat `--url` for endpoints in one Core instance or Enterprise
     cluster and pass `--edition core|enterprise`.
3. Select the database. External loads also require `create`, `reuse`, or an
   explicitly confirmed `reset`. Never infer reset authorization.
4. Keep durable writes and atomic batch rejection unless the user explicitly
   requests `--no-sync` or `--accept-partial`.
5. The manual profile defaults to 25,000-row batches and 16 load workers. For
   the measured non-durable throughput recipe, pass `--batch-size 3000`,
   `--load-workers 8`, and `--no-sync`; see
   `docs/influx3-ingestion-benchmark.md`.
6. Managed S3 settings come from the prepared workspace. Use
   `$setup-influxdb3 configure-s3`; never request credentials in conversation.

## Run benchmarks

Run from the repository root:

```bash
python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py all \
  --profile smoke --database-id core-311

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py all \
  --profile smoke --url http://127.0.0.1:8181 --edition enterprise \
  --database-mode create

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py generate \
  --only all --scale 2000000 --start 2023-06-11T00:00:00Z \
  --end 2023-06-11T00:05:00Z --compression gzip \
  --query-scope fixed-host

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py query \
  --profile smoke --url http://127.0.0.1:8181 --edition core \
  --query-count cpu-max-all-1=100 --query-count lastpoint=10

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py summarize \
  --run-dir .benchmarks/influxdb3/runs/RUN_ID
```

Repeat `--query-type` to define membership; omit it for every type allowed by
`--query-scope full|fixed-host` (default `full`). The fixed-host scope keeps
the 1/8-host CPU maximum and single-groupby queries plus `high-cpu-1`, and
rejects all-host selections. Recommend it at 10,000 hosts or more, while
requiring the explicit flag. `--queries=N` supplies a default count for
selected types, while repeatable
`--query-count TYPE=N` entries override individual counts. Without
`--query-type`, per-type entries define membership; with it, overrides must
name selected types. Each immutable query file executes once per run.
Query-only commands prepare logical dataset metadata without generating data.
Shared query sets are reused only after exact manifest, membership, size, and
checksum validation.
Data compression is opt-in with `--compression gzip`; plain remains the
default, compression is pinned by the run, and each compression has a distinct
dataset identity. Recommend gzip at 50 million estimated `cpu-only` points. The
loader consumes gzip through streaming decompression without creating a
temporary plain dataset.
Managed servers may take several minutes to initialize, so the runner waits up
to 10 minutes by default. Override this with `--startup-timeout SECONDS` when a
different allowance is required.

S3-backed managed servers pass the user-owned native credentials file directly
to InfluxDB and keep only logs locally. Credentials are never copied into run
metadata or environment variables. Bucket, region, endpoint, and HTTP allowance
are pinned by the workspace and reported without secret values.

## Authenticate external targets

Set `INFLUXDB3_AUTH_TOKEN` for writes and queries and
`INFLUXDB3_ADMIN_TOKEN` for database lifecycle operations. Override the
variable names with `--auth-token-env` and `--admin-token-env`. Token values
must never appear in reports, manifests, or command logs.

For multiple URLs, set query workers to at least the URL count when every node
must receive queries. The runner health-checks every URL and rejects conflicting
version, revision, or build metadata, but the user must confirm the URLs belong
to the same instance or cluster.

## Protect databases

- Give every managed workspace a stable `--database-id`; `--database-root`
  defaults to `.benchmarks/influxdb3/databases`.
- Reuse matching managed database bindings without loading duplicate data.
- Rebind only with `--database-mode reset --confirm-reset DATABASE` after the
  user explicitly authorizes deletion.
- Managed database workspaces remain locked while their server is running.
- Storage identity is immutable. Reset uses the database API and never directly
  empties an object-store bucket.
- External `reuse` may duplicate data; prefer query-only runs after one load.

## Report results

Read `summary.json` and report edition, version, database ID or URLs, sanitized
storage type/location, dataset and
query-set checksums, durability flags, metrics/second and rows/second, weighted
mean query latency, server diagnostics, failures and log paths, and the run
directory. Ordinary server warnings and recoverable errors are diagnostics;
fatal/panic output, startup failures, unexpected exits, and forced shutdowns
are benchmark failures.
