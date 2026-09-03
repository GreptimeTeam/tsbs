---
name: setup-greptimedb
description: Install checksum-verified GreptimeDB native releases and prepare reusable file- or S3-backed benchmark workspaces. Use for GreptimeDB installation, S3 configuration, exact-version setup, managed TSBS workspace preparation, verification, or locating a managed binary.
---

# Setup GreptimeDB

Use `scripts/setup.py` for deterministic installation and database management.
Read `references/setup.md` before choosing a version or platform. Use
`$benchmark-greptimedb` after preparing a database workspace.

## Install the latest stable or an exact version

Run from the repository root:

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py install

python3 .agents/skills/setup-greptimedb/scripts/setup.py install \
  --version 1.1.4

python3 .agents/skills/setup-greptimedb/scripts/setup.py install \
  --version v1.2.0-beta.1
```

Omitting `--version` resolves GitHub's latest stable, non-prerelease release.
Pass an exact version, with or without `v`, to select a stable or prerelease
version. The installer requires the adjacent vendor SHA-256, validates the
complete extracted distribution, checks `greptime --version`, and publishes
atomically. It never executes the official shell installer.

## Prepare a database workspace

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py prepare \
  --database-id greptime-114 --version 1.1.4

python3 .agents/skills/setup-greptimedb/scripts/setup.py prepare \
  --database-id greptime-stable
```

Existing workspaces are immutable with respect to version, platform, binary
checksum, and SQL database. Omitting `--version` resolves the stable version at
command execution time and never upgrades an existing workspace automatically.
Do not adopt or rewrite a legacy benchmark workspace; continue using it with an
explicit binary or choose a new database ID.

### Prepare S3 storage

Never ask the user to paste S3 credentials into conversation. Ask only for
non-secret values such as bucket, root, region, and endpoint, then give the
user a command to run in their own interactive terminal:

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py configure-s3 \
  --output /secure/path/greptimedb-s3.toml \
  --bucket BUCKET --root UNIQUE_ROOT --region REGION \
  --endpoint ENDPOINT
```

The command prompts without echo for the access key ID and secret access key,
creates an owner-only TOML without overwriting an existing file, and prints a
sanitized next command. If the user already maintains a full standalone TOML,
skip generation and use it directly:

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py prepare \
  --database-id greptime-s3 --greptime-config /secure/path/greptimedb-s3.toml
```

The config is live and user-owned. Its bucket/root identity is immutable for
the workspace, but credentials may be rotated in place. Do not read, quote, or
repeat credential-bearing config contents in conversation.

## Copy a loaded workspace for another version

Install the target version first, then create a fully independent database
copy:

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py install \
  --version 1.1.4

python3 .agents/skills/setup-greptimedb/scripts/setup.py copy \
  --source-database-id loaded-current \
  --database-id loaded-114 \
  --version 1.1.4
```

The source must be a loaded setup-managed workspace and the destination must
not exist. S3-backed workspaces cannot be copied; use a confirmed query-only
version override instead. For file storage, the command locks the source, fully copies `data/` without reflinks
or hard links, creates empty logs, preserves the SQL database and dataset
binding, records copy provenance, and publishes atomically. It never overwrites
or resumes a destination. Manually started GreptimeDB processes do not
participate in the cooperative workspace lock.

Use `$benchmark-greptimedb` to query the copied database ID. For an exact-path
comparison without a copy, use that skill's confirmed query-only runtime
version override instead.

## Inspect and verify

```bash
python3 .agents/skills/setup-greptimedb/scripts/setup.py list
python3 .agents/skills/setup-greptimedb/scripts/setup.py inspect --database-id greptime-114
python3 .agents/skills/setup-greptimedb/scripts/setup.py verify --database-id greptime-114
```

Report the database ID, exact version, platform, binary path and checksum, and
workspace path. Do not add the binary to `PATH` or alter system packages or
services.
