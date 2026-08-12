# GreptimeDB local setup reference

## Workspace

```text
.benchmarks/greptimedb/
├── installations/<version>/<platform>/{manifest.json,greptime,...}
└── databases/<database-id>/{manifest.json,data/,logs/}
```

Installations and prepared database workspaces are reusable and checksum
validated. Official assets use
`greptime-<os>-<arch>-v<version>.tar.gz` and an adjacent `.sha256sum` file on
GitHub Releases. The full extracted distribution is retained and hashed.

When `--version` is omitted, resolve GitHub's latest stable release endpoint.
The GreptimeDB download page and documentation quickstart are human-facing
cross-checks and can feature a prerelease. Resolution failures do not fall back
to the website, a cached version, or a guessed tag; pass an exact version for
offline or reproducible operation. Set `GITHUB_TOKEN` to authenticate GitHub
requests when anonymous API limits are insufficient. Never record the token.

Supported native platforms are Linux AMD64, Linux ARM64, macOS AMD64, and
macOS ARM64. Windows, Android, Docker, Kubernetes, package managers, and nightly
builds are outside this managed benchmark workflow.

Prepared manifests bind the database ID, SQL database name, release version,
platform, installation path, and binary checksum. Dataset binding remains owned
by the benchmark skill. Legacy benchmark manifests intentionally lack release
identity and require an explicit `--greptime-bin`.
