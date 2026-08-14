---
name: benchmark-greptimedb
description: Run repeatable GreptimeDB TSBS benchmarks with shared datasets, complete immutable query sets, reusable managed database workspaces, independent run logs, ingestion rates, and query-latency summaries. Use for GreptimeDB smoke or performance tests, ingestion measurements, query comparisons, managed local instances, or external GreptimeDB endpoints.
---

# Benchmark GreptimeDB

Use `scripts/benchmark.py` for execution and structured result parsing. Read
`references/workload.md` before selecting workload sizes, query types, database
modes, or shared workspace identifiers. Use `$generate-tsbs-data` for standalone
dataset generation, inspection, or non-Greptime serialization formats. Use
`$setup-greptimedb` to install and prepare a managed database workspace. Builds
automatically use `$setup-tsbs-environment` to reuse Go 1.21+ or prepare the
verified repository-local fallback.

## Collect inputs

1. Select a stage: `all`, `generate`, `load`, `query`, `summarize`, or
   `compare`.
2. For `all`, `load`, or `query`, select exactly one target:
   - managed: a prepared reusable `--database-id`; legacy workspaces also need
     an explicit GreptimeDB binary;
   - external: an HTTP endpoint.
3. Select the SQL `--database`. For external loads, also select `create`,
   `reuse`, or explicitly confirmed `reset`. Never infer reset authorization.
4. Use the `manual` profile unless the user requests `smoke` or overrides.

## Run benchmarks

Run from the repository root:

```bash
python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py all \
  --profile smoke --database-id smoke-db

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py generate \
  --profile smoke --only queries \
  --run-root .benchmarks/greptimedb/runs \
  --query-root .benchmarks/queries

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --profile smoke --endpoint http://127.0.0.1:4000 --database benchmark \
  --query-count cpu-max-all-1=100 --query-count lastpoint=10

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --database-id loaded-db --greptime-version 1.1.4 \
  --confirm-version-override loaded-db --dataset-id DATASET_ID

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py compare \
  --baseline-run .benchmarks/greptimedb/runs/BASELINE_RUN \
  --candidate-run .benchmarks/greptimedb/runs/CANDIDATE_RUN

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py summarize \
  --run-dir .benchmarks/greptimedb/runs/RUN_ID
```

Repeat `--query-type` to define query-set membership; omit it for every
supported type. `--queries=N` assigns a default count to every selected type.
Repeat `--query-count TYPE=N` to override individual counts; without
`--query-type`, those entries also define membership. With both flags, every
per-type override must name a selected type. Resolved counts are part of the
immutable query-set identity. Each selected query file is executed once. Start
another run to make another measurement.

For a cross-version query on the exact existing data directory, first install
the alternate release with `$setup-greptimedb`, then pass its exact
`--greptime-version` and repeat the database ID with
`--confirm-version-override`. This override is supported only by `query`, uses
the existing workspace lock, and does not rewrite the workspace's bound
installation identity. Use `--install-root` for a non-default managed install
root. Startup can still mutate persistent metadata, so treat confirmation as
authorization for that compatibility risk.

For an independent copy, use `$setup-greptimedb` to copy the loaded workspace
to a new database ID bound to the alternate release, then run a normal query
against the copy. The copy uses independent bytes and additional disk space.

Keep every version measurement in a separate run. Use `compare` with one
baseline and one or more repeated `--candidate-run` paths. Comparison requires
managed targets, complete successful query results, and identical SQL database,
dataset identity/checksum, query-set identity/checksum, membership, query
counts, and repetitions. Database IDs may differ. A valid comparison is
report-only and succeeds even when candidates regress.

Query-only commands prepare logical dataset metadata without generating data.
Use `--dataset-id` or `--dataset-path` to pin a dataset. Shared query sets live
under `--query-root` and are reused only after exact manifest, membership,
size, and checksum validation.

## Protect databases

- Give every managed workspace a stable `--database-id`; `--database-root`
  defaults to `.benchmarks/greptimedb/databases`.
- Prepare new managed workspaces with `$setup-greptimedb`; the benchmark runner
  verifies and discovers their version-bound binary automatically.
- Query-only version overrides resolve another checksum-validated managed
  installation and record both runtime and workspace-bound identities.
- Keep using `--greptime-bin` for legacy workspaces. Never silently adopt a
  legacy workspace into a downloaded installation.
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
For comparisons, report the comparison directory, baseline and candidate run
IDs and versions, improved/unchanged/regressed counts, the largest regression,
and per-query latency delta, percentage, and candidate/baseline ratio.
