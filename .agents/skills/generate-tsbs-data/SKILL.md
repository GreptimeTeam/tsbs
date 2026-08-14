---
name: generate-tsbs-data
description: Prepare logical TSBS dataset metadata and generate, cache, inspect, or verify reusable serialized dataset variants. Use for benchmark dataset identity, metadata-only query preparation, shared data generation, multiple database formats, cache inspection, or artifact checksum validation.
---

# Generate TSBS Data

Use `scripts/generate.py` for deterministic logical dataset identity,
generation, and validation. Read `references/datasets.md` when choosing
profiles, formats, or shared roots. Binary builds automatically use
`$setup-tsbs-environment` to reuse Go 1.21+ or prepare the verified
repository-local fallback.

## Prepare metadata only

Create or reuse a logical dataset without generating a serialization:

```bash
python3 .agents/skills/generate-tsbs-data/scripts/generate.py prepare \
  --profile smoke --dataset-root .benchmarks/datasets
```

Use this for query-only workflows. A logical dataset may contain only
`dataset.json`; no format is required.

## Generate or reuse data

```bash
python3 .agents/skills/generate-tsbs-data/scripts/generate.py generate \
  --profile smoke --format influx

python3 .agents/skills/generate-tsbs-data/scripts/generate.py generate \
  --profile manual --format timescaledb --dataset-root /shared/tsbs-data
```

The default root is `.benchmarks/datasets`. Set `TSBS_DATASET_ROOT` or pass
`--dataset-root` to share datasets. Use `--dataset-id` or `--dataset-path`, but
not both. Existing logical datasets inherit stored settings unless explicit
overrides conflict. Pass `--regenerate` only to intentionally replace a format
variant and `--rebuild` only to rebuild the generator. Reuse validates the
manifest, completion status, artifact presence, and byte size without rereading
the complete artifact to recompute its checksum.

## Inspect and verify

```bash
python3 .agents/skills/generate-tsbs-data/scripts/generate.py list
python3 .agents/skills/generate-tsbs-data/scripts/generate.py inspect --dataset-id DATASET_ID
python3 .agents/skills/generate-tsbs-data/scripts/generate.py verify \
  --dataset-id DATASET_ID --format influx
```

Use `--json` or `--result-file` for machine-readable output. Report the
dataset ID and specification; for materialized variants also report format,
data path, byte size, and recorded SHA-256. The `verify` command explicitly
recomputes the artifact checksum and should be used when full cache integrity
validation is required. Database-specific query generation belongs to the
corresponding benchmark skill.
