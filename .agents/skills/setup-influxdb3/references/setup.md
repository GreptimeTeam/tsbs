# InfluxDB 3 local setup reference

## Workspace

```text
.benchmarks/influxdb3/
├── installations/<edition>/<version>/<platform>/{manifest.json,influxdb3}
└── instances/<instance-id>/{manifest.json,data/,logs/}
```

Installations and instances are reusable and checksum validated. Core and
Enterprise artifacts use the official archive pattern
`influxdb3-<edition>-<version>_<platform>.tar.gz` and its adjacent `.sha256`.

Supported native platforms are Linux AMD64, Linux ARM64, and macOS ARM64.
Intel macOS and Windows are intentionally unsupported by the managed workflow.

## Instance defaults

- Bind HTTP to `127.0.0.1:8181` unless a different activation port is selected.
- Use `--object-store=file` and the instance `data/` directory.
- Derive a stable node ID from the instance ID.
- Add a distinct stable cluster ID for Enterprise.
- Use `--without-auth` for isolated managed benchmark instances.

Enterprise requires an active license. Trial/home activation supplies the email
and license type only to the server process; neither value is written to the
instance manifest. A license-file setup records only its absolute path and
never copies or reads the JWT contents.
