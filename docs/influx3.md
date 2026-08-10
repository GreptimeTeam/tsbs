# InfluxDB 3 Core and Enterprise

TSBS supports the `cpu-only` workload on InfluxDB 3 Core and Enterprise through
the native InfluxDB 3 HTTP APIs. Both products use the same TSBS commands and
SQL query set. The examples below target InfluxDB 3.11.0 on its default port,
8181.

## Build

```bash
make tsbs_generate_data tsbs_generate_queries \
  tsbs_load_influx3 tsbs_run_queries_influx3
```

## Generate and load data

Keep a Core dataset within its configured retention period. A 72-hour dataset
is a useful default for an unmodified Core installation.

```bash
tsbs_generate_data \
  --use-case=cpu-only \
  --seed=123 \
  --scale=1000 \
  --timestamp-start=2026-01-01T00:00:00Z \
  --timestamp-end=2026-01-04T00:00:00Z \
  --log-interval=10s \
  --format=influx3 | gzip > /tmp/influx3-data.gz

gunzip -c /tmp/influx3-data.gz | tsbs_load_influx3 \
  --urls=http://localhost:8181 \
  --db-name=benchmark \
  --auth-token="$INFLUXDB3_AUTH_TOKEN" \
  --admin-token="$INFLUXDB3_ADMIN_TOKEN" \
  --workers=4 \
  --batch-size=10000
```

The loader sends line protocol to `/api/v3/write_lp` with nanosecond
precision. Gzip is enabled, invalid batches are rejected atomically, and
durable WAL acknowledgement is used by default. Use `--no-sync` only for a
separately labelled durability/performance experiment, and use
`--accept-partial` only if partial batch acceptance is intentional.

By default the loader creates the benchmark database, replacing one with the
same name. Set `--do-abort-on-exist` to stop instead of replacing it.
Database lifecycle calls use `/api/v3/configure/database`. If `--admin-token`
is omitted, it falls back to `--auth-token`. To use a pre-created database and
a database-scoped write token, pass `--do-create-db=false` and
`--do-abort-on-exist=false`; no admin token is then required.

Multiple comma-separated `--urls` are assigned to workers round-robin. Use
only endpoints that address the same Core instance or Enterprise cluster.

## Generate and run queries

InfluxDB 3 queries are native SQL requests sent to `/api/v3/query_sql`. The
generated last-point query uses ordered aggregates such as
`last_value(usage_user ORDER BY time)`, grouped by hostname.

```bash
tsbs_generate_queries \
  --use-case=cpu-only \
  --seed=123 \
  --scale=1000 \
  --timestamp-start=2026-01-01T00:00:00Z \
  --timestamp-end=2026-01-04T00:00:01Z \
  --queries=1000 \
  --query-type=lastpoint \
  --format=influx3 | gzip > /tmp/queries_influx3_lastpoint.gz

gunzip -c /tmp/queries_influx3_lastpoint.gz | \
  tsbs_run_queries_influx3 \
    --urls=http://localhost:8181 \
    --db-name=benchmark \
    --auth-token="$INFLUXDB3_AUTH_TOKEN" \
    --workers=4
```

The supported CPU query types are the standard TSBS devops set, including
time-window aggregates, grouped aggregates, last point per host, and high-CPU
filters. `scripts/generate_queries.sh` can generate several types by setting
`FORMATS=influx3`; the helper scripts `scripts/load/load_influx3.sh` and
`scripts/run_queries/run_queries_influx3.sh` run the load and query phases.

## Comparing Core and Enterprise

Use identical generated files, client concurrency, batch sizes, durability
settings, and query order for both products. Record the exact 3.11 patch
version and the storage engine used by each Enterprise database: upgraded
Enterprise installations can retain an older engine while new 3.11 databases
use the newer engine by default. Do not combine `--no-sync` results with the
default durable-write results.
