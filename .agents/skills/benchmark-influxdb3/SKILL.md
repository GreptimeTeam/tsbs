---
name: benchmark-influxdb3
description: Run repeatable InfluxDB 3 Core and Enterprise TSBS benchmarks with shared datasets, immutable SQL query sets, reusable managed database workspaces, external clusters, independent logs, ingestion rates, and query-latency summaries. Use for InfluxDB 3 smoke or performance tests, Core-versus-Enterprise measurements, ingestion tests, query comparisons, or external InfluxDB 3 endpoints.
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

## Run benchmarks

Run from the repository root:

```bash
python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py all \
  --profile smoke --database-id core-311

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py all \
  --profile smoke --url http://127.0.0.1:8181 --edition enterprise \
  --database-mode create

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py query \
  --profile smoke --url http://127.0.0.1:8181 --edition core \
  --query-type cpu-max-all-1 --query-type lastpoint

python3 .agents/skills/benchmark-influxdb3/scripts/benchmark.py summarize \
  --run-dir .benchmarks/influxdb3/runs/RUN_ID
```

Repeat `--query-type` to define membership; omit it for every supported type.
Each immutable query file executes once per run. Query-only commands prepare
logical dataset metadata without generating data. Shared query sets are reused
only after exact manifest, membership, size, and checksum validation.
Managed servers may take several minutes to initialize, so the runner waits up
to 10 minutes by default. Override this with `--startup-timeout SECONDS` when a
different allowance is required.

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
- External `reuse` may duplicate data; prefer query-only runs after one load.

## Report results

Read `summary.json` and report edition, version, database ID or URLs, dataset and
query-set checksums, durability flags, metrics/second and rows/second, weighted
mean query latency, server diagnostics, failures and log paths, and the run
directory. Ordinary server warnings and recoverable errors are diagnostics;
fatal/panic output, startup failures, unexpected exits, and forced shutdowns
are benchmark failures.
