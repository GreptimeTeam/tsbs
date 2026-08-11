---
name: benchmark-greptimedb
description: Run repeatable GreptimeDB benchmarks with TSBS, including shared dataset reuse, targeted TSBS builds, query generation, ingestion, individual or repeated query workloads, isolated local GreptimeDB lifecycle management, external endpoint testing, durable logs, and ingestion/query summaries. Use for GreptimeDB performance tests, TSBS smoke tests, ingestion-rate measurements, query-latency comparisons, or rerunning selected TSBS queries against an existing database.
---

# Benchmark GreptimeDB

Use the bundled runner for deterministic execution and log parsing. Read
`references/workload.md` when selecting workload sizes, query types, or database
modes. Use `$generate-tsbs-data` for standalone data generation, cache
inspection, or non-Greptime serialization formats.

## Collect required inputs

1. Ask which stage to run: `all`, `generate`, `load`, `query`, or `summarize`.
2. For `all`, `load`, or `query`, ask for exactly one connection:
   - a GreptimeDB binary path for an isolated managed instance; or
   - an HTTP endpoint for an existing instance.
3. For an external load, ask for the database name and one database mode:
   `create`, `reuse`, or `reset`. Never infer `reset`.
4. For a query against an existing database without a prior run manifest, ask
   for its timestamp range and host scale so generated queries match the data.
5. Default to the `manual` profile unless the user requests `smoke` or explicit
   overrides.

## Run benchmarks

Invoke `scripts/benchmark.py` from the repository root. Examples:

```bash
python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py all \
  --profile smoke --greptime-bin /absolute/path/to/greptime

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py generate \
  --profile smoke --only data --dataset-root /shared/tsbs-data

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py load \
  --run-dir .benchmarks/greptimedb/RUN_ID \
  --endpoint http://127.0.0.1:4000 --database benchmark \
  --database-mode reuse

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py query \
  --run-dir .benchmarks/greptimedb/RUN_ID \
  --endpoint http://127.0.0.1:4000 --database benchmark \
  --query-type cpu-max-all-1 --repeat 3

python3 .agents/skills/benchmark-greptimedb/scripts/benchmark.py summarize \
  --run-dir .benchmarks/greptimedb/RUN_ID
```

Repeat `--query-type` to select multiple query types. Use `--regenerate` only
when intentionally replacing a shared data variant or query input. Use
`--dataset-id` or `--dataset-path` to pin a reusable dataset. Use `--rebuild`
only when existing TSBS binaries must be rebuilt.

## Protect databases

- Prefer `reuse` to load into an existing database without dropping it. This
  maps to TSBS `--do-create-db=false` and can duplicate data if repeated.
- Use `create` for a missing database; it aborts if the database exists.
- Use `reset` only after the user explicitly authorizes dropping the named
  database. Pass `--confirm-reset DATABASE`, which must exactly match
  `--database`.
- Treat `query` and `summarize` as non-mutating database operations.
- Do not claim external authentication support; the current TSBS query runner
  does not send GreptimeDB Basic authentication.

## Report results

After a run, read `summary.md` and report:

- metrics/second and rows/second for every ingestion attempt;
- weighted mean latency in milliseconds for every query type;
- failed or unparsable attempts and their log paths;
- the dataset ID and checksum; and
- the run directory so the user can inspect raw logs and `summary.json`.

Never discard failed-run artifacts. Managed mode stops only the process it
started and retains its data for later query-only runs.
