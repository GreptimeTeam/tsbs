# GreptimeDB TSBS workload reference

## Shared workspace

```text
.benchmarks/
├── datasets/<dataset-id>/...
├── queries/<dataset-id>/greptime/<query-set-id>/
│   ├── manifest.json
│   └── queries/<query-type>.dat
└── greptimedb/
    ├── installations/<version>/<platform>/{manifest.json,greptime,...}
    ├── databases/<database-id>/{manifest.json,data/,logs/}
    └── runs/<run-id>/{manifest.json,logs/,results/,summary.json,summary.md}
```

A query-set identity includes the logical dataset identity and specification,
Greptime query format, use case, seed, timestamp range, and the sorted
query-type-to-count map. A subset is a complete set with only those files.
Generation publishes the directory atomically; generator commands and stderr
remain in the initiating run rather than the shared set.

## Profiles

Both profiles use seed `123`, interval `10s`, data use case `cpu-only`, query
use case `devops`, Influx line protocol data, and Greptime query format.

| Setting | `manual` (default) | `smoke` |
| --- | --- | --- |
| Start | `2023-06-11T00:00:00Z` | `2023-06-11T00:00:00Z` |
| End | `2023-06-14T00:00:00Z` | `2023-06-12T00:00:00Z` |
| Hosts | 4000 | 10 |
| Load workers | 6 | 2 |
| Query workers | 1 | 1 |
| Batch size | 3000 | 3000 |

The query generator receives the end timestamp plus one second.

## Query counts

| Query type | Manual | Smoke |
| --- | ---: | ---: |
| `cpu-max-all-1` | 100 | 10 |
| `cpu-max-all-8` | 100 | 10 |
| `double-groupby-1` | 50 | 10 |
| `double-groupby-5` | 50 | 10 |
| `double-groupby-all` | 50 | 10 |
| `groupby-orderby-limit` | 50 | 10 |
| `high-cpu-1` | 100 | 10 |
| `high-cpu-all` | 50 | 10 |
| `lastpoint` | 10 | 10 |
| `single-groupby-1-1-1` | 100 | 10 |
| `single-groupby-1-1-12` | 100 | 10 |
| `single-groupby-1-8-1` | 100 | 10 |
| `single-groupby-5-1-1` | 100 | 10 |
| `single-groupby-5-1-12` | 100 | 10 |
| `single-groupby-5-8-1` | 100 | 10 |

## Database state

Managed workspace manifests bind `database_id`, SQL database name, and one
loaded dataset specification/checksum. Workspaces prepared by
`$setup-greptimedb` additionally bind an exact installation version, platform,
path, and binary checksum. A matching dataset is reused. A
different dataset requires a successfully confirmed reset before the binding
changes. External `create`, `reuse`, and `reset` map to the corresponding TSBS
loader flags; external reuse may duplicate data.
