# TSBS shared dataset reference

## Layout and identity

```text
.benchmarks/datasets/<dataset-id>/
├── dataset.json
└── formats/<format>/
    ├── data
    ├── generate.log
    └── manifest.json
```

Only `dataset.json` is required. Metadata-only preparation deliberately leaves
`formats/` absent. The dataset ID identifies logical points and excludes
serialization format. Its canonical specification contains `use_case`,
`seed`, `scale`, `start`, `end`, and `log_interval`.

Each format directory contains one serialization and records status, byte
size, SHA-256, generator binary checksum, Git revision, and timestamps.

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

## Formats and safety

The generator validates format names. GreptimeDB and InfluxDB 3 consume the
`influx` variant; other loaders should request their native TSBS format under
the same logical dataset ID.

- Validate the logical manifest, completion status, artifact presence, and byte
  size before ordinary reuse. Run `generate.py verify` to recompute and validate
  the artifact checksum explicitly.
- Publish a replacement payload only after successful generation.
- Preserve the completed artifact when regeneration fails.
- Keep cached data outside Git; `.benchmarks/` is ignored.
