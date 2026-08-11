# GreptimeDB TSBS workload reference

## Profiles

Both profiles use seed `123`, a `10s` data interval, the `cpu-only` data use
case, the `devops` query use case, Influx line protocol input, and GreptimeDB
query format.

| Setting | `manual` (default) | `smoke` |
| --- | --- | --- |
| Start | `2023-06-11T00:00:00Z` | `2023-06-11T00:00:00Z` |
| End | `2023-06-14T00:00:00Z` | `2023-06-12T00:00:00Z` |
| Hosts (`scale`) | 4000 | 10 |
| Load workers | 6 | 2 |
| Query workers | 1 | 1 |
| Batch size | 3000 | 3000 |

The query generator receives the end timestamp plus one second, matching the
original benchmark manual.

## Query types and manual counts

| Query type | Count |
| --- | ---: |
| `cpu-max-all-1` | 100 |
| `cpu-max-all-8` | 100 |
| `double-groupby-1` | 50 |
| `double-groupby-5` | 50 |
| `double-groupby-all` | 50 |
| `groupby-orderby-limit` | 50 |
| `high-cpu-1` | 100 |
| `high-cpu-all` | 50 |
| `lastpoint` | 10 |
| `single-groupby-1-1-1` | 100 |
| `single-groupby-1-1-12` | 100 |
| `single-groupby-1-8-1` | 100 |
| `single-groupby-5-1-1` | 100 |
| `single-groupby-5-1-12` | 100 |
| `single-groupby-5-8-1` | 100 |

The smoke profile runs 10 queries of every type.

## Database modes

- `create`: pass `--do-create-db=true --do-abort-on-exist=true`. Create a
  missing database and fail rather than drop an existing database.
- `reuse`: pass `--do-create-db=false`. Load into the named existing database
  without creating or dropping it.
- `reset`: pass `--do-create-db=true`, allowing TSBS to drop and recreate the
  database. Require `--confirm-reset` to exactly match the database name.

Repeated `reuse` loads can duplicate data. Prefer query-only repetitions after
one successful ingestion.
