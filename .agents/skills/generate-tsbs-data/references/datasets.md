# TSBS shared dataset reference

## Cache layout

```text
.benchmarks/datasets/<dataset-id>/
├── dataset.json
└── formats/<format>/
    ├── data
    ├── generate.log
    └── manifest.json
```

The dataset ID identifies logical points and excludes serialization format.
Its normalized specification contains `use_case`, `seed`, `scale`, `start`,
`end`, and `log_interval`. Each format directory contains one serialization of
those points and records its own checksum and generator provenance.

## Profiles

| Setting | `manual` | `smoke` |
| --- | --- | --- |
| Start | `2023-06-11T00:00:00Z` | `2023-06-11T00:00:00Z` |
| End | `2023-06-14T00:00:00Z` | `2023-06-12T00:00:00Z` |
| Hosts | 4000 | 10 |
| Seed | 123 | 123 |
| Interval | 10s | 10s |
| Use case | cpu-only | cpu-only |

`manual` is the default. Explicit flags override profile values.

## Formats and reuse

Pass any format accepted by `tsbs_generate_data`. The generator remains the
authoritative format validator. Use the same format variant only when the
database loader accepts the exact serialization.

GreptimeDB and InfluxDB 3 both use the Influx line-protocol serializer in this
repository, so both should request `--format influx`. Other databases should
request their native TSBS format under the same logical dataset ID.

## Safety

- Validate the manifest and SHA-256 checksum before every reuse.
- Treat `--regenerate` as an explicit replacement of a shared artifact.
- Publish a new payload only after generation succeeds.
- Retain generation logs after failures.
- Keep cached data out of Git; `.benchmarks/` is ignored.
