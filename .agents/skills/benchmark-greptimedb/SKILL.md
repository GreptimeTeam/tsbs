---
name: benchmark-greptimedb
description: Run repeatable GreptimeDB TSBS benchmarks and collect cold and hot EXPLAIN ANALYZE VERBOSE results with shared datasets, immutable query sets, reusable managed database workspaces, independent run logs, ingestion rates, and query summaries. Use for GreptimeDB smoke or performance tests, query analysis, ingestion measurements, query comparisons, managed local instances, or external GreptimeDB endpoints.
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

1. Select a stage: `all`, `generate`, `load`, `query`, `analyze`, `summarize`,
   or `compare`.
2. For `all`, `load`, or `query`, select exactly one target:
   - managed: a prepared reusable `--database-id`; optionally select an
     explicit `--greptime-bin`, which legacy workspaces require;
   - external: an HTTP endpoint.
   `analyze` requires a managed target because it restarts GreptimeDB before
   every selected query type.
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

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py generate \
  --only all --scale 1000000 --start 2023-06-11T00:00:00Z \
  --end 2023-06-11T00:10:00Z --compression gzip \
  --query-scope fixed-host

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --profile smoke --endpoint http://127.0.0.1:4000 --database benchmark \
  --query-count cpu-max-all-1=100 --query-count lastpoint=10

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --database-id loaded-db --greptime-version 1.1.4 \
  --confirm-version-override loaded-db --dataset-id DATASET_ID

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --database-id loaded-db --greptime-bin ./target/release/greptime \
  --dataset-id DATASET_ID

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py analyze \
  --profile smoke --database-id loaded-db --hot-runs 2

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --database-id loaded-db \
  --greptime-config .benchmarks/greptimedb/configs/scan-1024.toml

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py compare \
  --baseline-run .benchmarks/greptimedb/runs/BASELINE_RUN \
  --candidate-run .benchmarks/greptimedb/runs/CANDIDATE_RUN

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py summarize \
  --run-dir .benchmarks/greptimedb/runs/RUN_ID
```

### Custom managed configuration

When the user requests GreptimeDB settings but does not provide a TOML file,
read GreptimeDB's official [standalone example](https://github.com/GreptimeTeam/greptimedb/blob/main/config/standalone.example.toml)
before writing the config. The full set of examples is in the upstream
[config directory](https://github.com/GreptimeTeam/greptimedb/tree/main/config).
For a release runtime, replace `main` in the standalone URL with the exact
`vVERSION` tag selected by the prepared workspace or `--greptime-version`.
Use `main` only for a main or nightly runtime; if the matching release template
is unavailable, report that instead of silently consulting a different
version.

Create a minimal file under
`.benchmarks/greptimedb/configs/DESCRIPTIVE_NAME.toml` containing only the
requested overrides, with the same table nesting and value types as the
matching upstream example. Do not copy all default settings. For example:

```toml
[[region_engine]]
[region_engine.mito]
max_concurrent_scan_files = 1024
```

Pass the file with `--greptime-config`. It is a live source file: the run
records its resolved path but does not copy or checksum-pin its contents, and
later starts read its current contents. Resuming the same run without the flag
reuses the recorded path; selecting another path requires a new run. The
runner's CLI values for HTTP address, InfluxDB enablement, data home, and log
directory override conflicting values in the TOML. Custom configs apply only
to managed targets, not `--endpoint`.

Repeat `--query-type` to define query-set membership; omit it for every type
allowed by `--query-scope full|fixed-host` (default `full`). The fixed-host
scope keeps the 1/8-host CPU maximum and single-groupby queries plus
`high-cpu-1`, and rejects all-host selections. Recommend it at 10,000 hosts or
more, but require the explicit flag. `--queries=N` assigns a default count to
every selected type.
Repeat `--query-count TYPE=N` to override individual counts; without
`--query-type`, those entries also define membership. With both flags, every
per-type override must name a selected type. Resolved counts are part of the
immutable query-set identity. Each selected query file is executed once. Start
another run to make another measurement.

`analyze` executes the first generated SQL of each selected type as a cold
`EXPLAIN ANALYZE VERBOSE` query immediately after a managed GreptimeDB restart.
It then executes the next generated query records without restarting; these are
the hot runs. Predicate-based types produce distinct SQL, while invariant types
such as `lastpoint` repeat their SQL. `--hot-runs` defaults to `2` and must be
positive. Every selected query type must contain at least `1 + hot-runs`
generated queries. A cold run does not clear operating-system filesystem
caches.

Analysis results live under
`results/analyze/QUERY_TYPE/run-NNN/{cold.json,hot-NNN.json,metrics.json}`.
The matching runner and GreptimeDB process logs live under
`logs/analyze/QUERY_TYPE/run-NNN/`. Repeating analysis in an existing run uses
the next attempt directory and does not overwrite earlier results. The `all`
stage does not run analysis.

For a cross-version query or analysis on the exact existing data directory,
first install the alternate release with `$setup-greptimedb`, then pass its exact
`--greptime-version` and repeat the database ID with
`--confirm-version-override`. This override is supported by `query` and
`analyze`, uses the existing workspace lock, and does not rewrite the
workspace's bound installation identity. Use `--install-root` for a non-default
managed install root. Startup can still mutate persistent metadata, so treat
confirmation as authorization for that compatibility risk.

Use `--greptime-bin PATH` to run `all`, `load`, `query`, or `analyze` with an
arbitrary local GreptimeDB build against a managed workspace. The explicit path
takes precedence over the workspace-bound installation and is itself
authorization to start that binary on the workspace; no additional confirmation
is required. The runner resolves the path, requires an executable whose
`--version` output contains a semantic GreptimeDB version, and records its path,
version, and SHA-256 alongside the workspace-bound identity. It does not rewrite
the workspace manifest. The path and checksum are pinned by the run, so pass the
same unchanged binary when resuming. `--greptime-bin` cannot be combined with
`--greptime-version` or an external `--endpoint`.

For an independent copy, use `$setup-greptimedb` to copy the loaded workspace
to a new database ID bound to the alternate release, then run a normal query
against the copy. The copy uses independent bytes and additional disk space.

Keep every version measurement in a separate run. Use `compare` with one
baseline and one or more repeated `--candidate-run` paths. Comparison requires
managed targets, complete successful query results, and identical SQL database,
dataset identity/checksum, query-set identity/checksum, membership, query
counts, and repetitions. Database IDs may differ. A valid comparison is
report-only and succeeds even when candidates regress. Custom config paths may
differ and are reported rather than treated as comparison compatibility fields.

Query and analysis commands prepare logical dataset metadata without generating data.
Use `--dataset-id` or `--dataset-path` to pin a dataset. Shared query sets live
under `--query-root` and are reused only after exact manifest, membership,
size, and checksum validation.

Data compression is opt-in with `--compression gzip`; plain remains the
default, compression is pinned by the run, and each compression has a distinct
dataset identity. Recommend gzip when the
`cpu-only` estimate reaches 50 million points. Compressed data is decompressed
directly into the loader without a temporary plain file.

## Protect databases

- Give every managed workspace a stable `--database-id`; `--database-root`
  defaults to `.benchmarks/greptimedb/databases`.
- Prepare new managed workspaces with `$setup-greptimedb`; the benchmark runner
  verifies and discovers their version-bound binary automatically.
- Query and analysis version overrides resolve another checksum-validated
  managed installation and record both runtime and workspace-bound identities.
- Explicit `--greptime-bin` overrides are supported for every managed execution
  stage and record both runtime and workspace-bound identities. Keep using them
  for legacy workspaces; never silently adopt legacy state into an installation.
- Keep one SQL database and one loaded dataset per managed workspace. Reuse a
  matching binding without loading duplicate data.
- Rebind only with `--database-mode reset --confirm-reset DATABASE`, after the
  user explicitly authorizes dropping that SQL database.
- Managed workspaces are locked while GreptimeDB uses them.
- For external loads, `reuse` can duplicate data. Prefer query-only runs after
  one successful load.

## Report results

Read `summary.json` and report the dataset ID and checksum, query-set ID and
manifest checksum, database ID or external target, custom config path when
present, metrics/second and
rows/second for ingestion, weighted mean latency per query type, failures and
their log paths, and the run directory. Preserve failed-run diagnostics.
For analysis, also report each query type's attempt and cold/hot result paths,
runner log, and GreptimeDB process log. Preserve the full GreptimeDB response;
verbose plan fields are diagnostic text and should not be parsed as a stable
metrics API.
For comparisons, report the comparison directory, baseline and candidate run
IDs and versions, improved/unchanged/regressed counts, the largest regression,
and per-query latency delta, percentage, and candidate/baseline ratio.
