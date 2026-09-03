# GreptimeDB managed setup reference

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
platform, installation path, binary checksum, and storage identity. Manifests
without storage metadata are file-backed for compatibility. S3 manifests record
the live TOML path and non-secret bucket/root settings; credentials remain only
in that file. Dataset binding remains owned
by the benchmark skill. Legacy benchmark manifests intentionally lack release
identity and require an explicit `--greptime-bin`.

A copied workspace is prepared from a loaded managed source while holding the
same cooperative directory lock used by the benchmark runner. Only `data/` is
copied; logs start empty. The destination uses a new database ID and target
installation identity while preserving the source SQL database and dataset
binding. Copying is full and independent: symbolic links and other special
entries in `data/` are rejected, and reflinks and hard links are not used. The
destination is staged beside the database root and atomically published only
after file counts and byte counts match.

Copy provenance records the source database ID and path, version and binary
checksum, source manifest checksum, timestamp, method, file count, and byte
count. The target version must be exact and already installed. Copying does not
assert that GreptimeDB supports downgrade or upgrade startup for those data
files.

S3-backed workspaces cannot use `copy` because their primary bytes are not in
the local `data/` directory. That directory remains available for WAL and cache
state. Credential-bearing TOML files are user-owned, never copied or
checksummed, and must not be printed or requested through conversation.
