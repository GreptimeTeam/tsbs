# TSBS shared dataset reference

## Layout and identity

```text
.benchmarks/datasets/<dataset-id>/
├── dataset.json
└── formats/<format>/
    ├── data[.gz]
    ├── generate.log
    └── manifest.json
```

Only `dataset.json` is required. Metadata-only preparation deliberately leaves
`formats/` absent. The dataset ID includes the logical point specification and
compression, but excludes database serialization format. Its canonical point
specification contains `use_case`, `seed`, `scale`, `start`, `end`, and
`log_interval`.

Each dataset selects one compression and each format directory contains one
serialization. The manifests record status, compression, canonical
uncompressed size/SHA-256, stored artifact size/SHA-256, generator binary
checksum, Git revision, and timestamps. Existing schema-v1 datasets without a
compression field remain valid plain datasets.

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
- Compression is part of dataset identity. `none` is the default and preserves
  existing automatic dataset IDs; `gzip` is deterministic and uses a distinct
  dataset directory.
- For `cpu-only`, estimated points equal
  `scale × floor((end - start) / log_interval)`. Recommend gzip from 50 million
  points. A 100-host, one-hour Influx sample measured about 344 bytes/point
  plain and 36 bytes/point compressed; actual formats and values vary.
