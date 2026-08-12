# InfluxDB 3 TSBS workload reference

## Shared workspace

```text
.benchmarks/
├── datasets/<dataset-id>/formats/influx/...
├── queries/<dataset-id>/influx3/<query-set-id>/...
└── influxdb3/
    ├── installations/...
    ├── instances/<instance-id>/{manifest.json,data/,logs/}
    └── runs/<run-id>/{manifest.json,logs/,results/,summary.json,summary.md}
```

Both Core and Enterprise consume the same `influx` line-protocol dataset and
native InfluxDB 3 SQL query set. Reuse identical artifacts, query order,
workers, batch sizes, and durability flags for comparisons.

## Profiles

| Setting | `manual` (default) | `smoke` |
| --- | --- | --- |
| Start | `2023-06-11T00:00:00Z` | `2023-06-11T00:00:00Z` |
| End | `2023-06-14T00:00:00Z` | `2023-06-12T00:00:00Z` |
| Hosts | 4000 | 10 |
| Load workers | 6 | 2 |
| Query workers | 1 | 1 |
| Batch size | 3000 | 3000 |

Both use seed `123`, interval `10s`, `cpu-only` data, `devops` queries,
durable WAL acknowledgement, and rejection of partial batches. The query end
timestamp is the dataset end plus one second.

## Database and target state

Managed instance manifests pin edition, exact version, binary checksum,
node/cluster identity, SQL database, and one loaded dataset checksum. External
targets require an explicit edition and may provide multiple URLs only when all
URLs address the same Core instance or Enterprise cluster. The runner compares
available `/ping` version metadata but cannot prove cluster membership. Use at
least as many query workers as URLs to exercise every endpoint.

`create` aborts if the database exists, `reuse` leaves it intact, and `reset`
deletes and recreates it only after exact name confirmation. Never compare a
`--no-sync` or `--accept-partial` run with the durable default without clearly
labeling the difference.

Before binding a managed instance to a SQL database, the runner verifies that
its pinned executable starts for `--version` and reports the expected edition
and exact version. Repair incomplete installations with `$setup-influxdb3`.
