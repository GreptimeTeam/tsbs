---
name: benchmark-greptimedb
description: Run repeatable GreptimeDB TSBS benchmarks with shared datasets, complete immutable query sets, reusable managed database workspaces, independent run logs, ingestion rates, and query-latency summaries. Use for GreptimeDB smoke or performance tests, ingestion measurements, query comparisons, managed local instances, or external GreptimeDB endpoints.
---

# Benchmark GreptimeDB

Use `scripts/benchmark.py` for execution and structured result parsing. Read
`references/workload.md` before selecting workload sizes, query types, database
modes, or shared workspace identifiers. Use `$generate-tsbs-data` for standalone
dataset generation, inspection, or non-Greptime serialization formats.

## Collect inputs

1. Select a stage: `all`, `generate`, `load`, `query`, or `summarize`.
2. For `all`, `load`, or `query`, select exactly one target:
   - managed: an executable GreptimeDB binary plus a reusable `--database-id`;
   - external: an HTTP endpoint.
3. Select the SQL `--database`. For external loads, also select `create`,
   `reuse`, or explicitly confirmed `reset`. Never infer reset authorization.
4. Use the `manual` profile unless the user requests `smoke` or overrides.

## Run benchmarks

Run from the repository root:

```bash
python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py all \
  --profile smoke --greptime-bin /absolute/path/to/greptime \
  --database-id smoke-db

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py generate \
  --profile smoke --only queries \
  --run-root .benchmarks/greptimedb/runs \
  --query-root .benchmarks/queries

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --profile smoke --endpoint http://127.0.0.1:4000 --database benchmark \
  --query-type cpu-max-all-1 --query-type lastpoint

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py summarize \
  --run-dir .benchmarks/greptimedb/runs/RUN_ID
```

Repeat `--query-type` to define query-set membership; omit it for every
supported type. `--queries=N` assigns that count to every selected type and
therefore selects a different immutable query set. Each selected query file is
executed once. Start another run to make another measurement.

Query-only commands prepare logical dataset metadata without generating data.
Use `--dataset-id` or `--dataset-path` to pin a dataset. Shared query sets live
under `--query-root` and are reused only after exact manifest, membership,
size, and checksum validation.

## Protect databases

- Give every managed workspace a stable `--database-id`; `--database-root`
  defaults to `.benchmarks/greptimedb/databases`.
- Keep one SQL database and one loaded dataset per managed workspace. Reuse a
  matching binding without loading duplicate data.
- Rebind only with `--database-mode reset --confirm-reset DATABASE`, after the
  user explicitly authorizes dropping that SQL database.
- Managed workspaces are locked while GreptimeDB uses them.
- For external loads, `reuse` can duplicate data. Prefer query-only runs after
  one successful load.

## Report results

Read `summary.json` and report the dataset ID and checksum, query-set ID and
manifest checksum, database ID or external target, metrics/second and
rows/second for ingestion, weighted mean latency per query type, failures and
their log paths, and the run directory. Preserve failed-run diagnostics.
