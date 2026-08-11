---
name: generate-tsbs-data
description: Generate, cache, inspect, and verify reusable TSBS benchmark datasets in any tsbs_generate_data format. Use for creating benchmark input, reusing identical data across database benchmarks, preparing multiple serialization variants of one logical workload, or validating cached TSBS data before loading it.
---

# Generate TSBS Data

Use `scripts/generate.py` for deterministic dataset generation and cache
validation. Read `references/datasets.md` when choosing profiles, formats, or
shared cache locations.

## Generate or reuse data

Run the script from the TSBS repository root:

```bash
python3 .agents/skills/generate-tsbs-data/scripts/generate.py generate \
  --profile smoke --format influx

python3 .agents/skills/generate-tsbs-data/scripts/generate.py generate \
  --profile manual --format timescaledb --dataset-root /shared/tsbs-data
```

The default cache is `.benchmarks/datasets`. Set `TSBS_DATASET_ROOT` or pass
`--dataset-root` to share datasets across repositories or machines. Generation
builds only `tsbs_generate_data` when its binary is missing; pass `--rebuild`
to rebuild it intentionally.

Use `--dataset-id` to select a named entry under the dataset root or
`--dataset-path` to select an exact directory. Never pass both. Existing
datasets inherit their stored workload unless explicit overrides are supplied;
conflicting overrides fail. Format variants are verified before reuse. Pass
`--regenerate` only when intentionally replacing a format variant.

## Inspect cached data

```bash
python3 .agents/skills/generate-tsbs-data/scripts/generate.py list
python3 .agents/skills/generate-tsbs-data/scripts/generate.py inspect --dataset-id DATASET_ID
python3 .agents/skills/generate-tsbs-data/scripts/generate.py verify \
  --dataset-id DATASET_ID --format influx
```

Use `--json` when another script needs machine-readable output. Report the
dataset ID, format, data path, and SHA-256 checksum to callers.

## Share across databases

Keep one logical dataset ID for the same use case, seed, scale, timestamp range,
and interval. Generate separate format variants only when loaders need distinct
serialization. GreptimeDB and InfluxDB 3 can both consume the canonical
`influx` variant; query generation remains the responsibility of each database
benchmark skill.
